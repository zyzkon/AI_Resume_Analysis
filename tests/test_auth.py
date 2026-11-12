"""认证链路：注册的输入把关、两种登录入口、401 的三种文案、账号状态的影响。

断言的写法都对着「攻击者能观察到什么」：邮箱不存在和密码错误必须返回完全一致的
状态码和文案，否则拿一个邮箱试一次就能判断它注册过没有。

这一组不需要预先造简历，用 conftest 里的 `user` / `auth_headers`
（它们直接调 security 和 ORM，省掉注册+登录两次 HTTP 往返）。
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.user import User

EMAIL = "tester@example.com"


def _register(client: TestClient, email: str = "new@example.com", password: str = "Passw0rd!23"):
    return client.post("/auth/register", json={"email": email, "password": password})


# ---------------------------------------------------------------- 注册


def test_register_returns_user_without_password(client: TestClient) -> None:
    """201 + 用户信息，且响应里绝不能出现任何密码相关字段。

    UserOut 是白名单模型（列了什么返回什么），这条用例就是在钉死这一点：
    哪天有人图省事把 ORM 对象直接 return，hashed_password 会立刻泄出去。
    """
    response = _register(client, email="NewUser@Example.COM")

    assert response.status_code == 201
    body = response.json()
    # 邮箱在 schema 层统一转小写，保证登录时 WHERE email = ? 能命中
    assert body["email"] == "newuser@example.com"
    assert body["is_active"] is True
    assert body["id"] > 0
    assert "password" not in body
    assert "hashed_password" not in body


def test_register_duplicate_email_returns_409(client: TestClient) -> None:
    """重复注册靠数据库唯一索引裁决，不是「先查后插」。

    大小写不同也算重复 —— 入库前已经统一转小写了。
    """
    assert _register(client, email="dup@example.com").status_code == 201

    response = _register(client, email="DUP@example.com")

    assert response.status_code == 409
    assert response.json()["detail"] == "该邮箱已被注册"


def test_register_rejects_short_password(client: TestClient) -> None:
    response = _register(client, password="short")

    assert response.status_code == 422


def test_register_rejects_invalid_email(client: TestClient) -> None:
    response = _register(client, email="not-an-email")

    assert response.status_code == 422


def test_register_limits_password_by_bytes_not_chars(client: TestClient) -> None:
    """25 个汉字 = 75 字节，字符数只有 25（没超 max_length=64），但已超 bcrypt 的 72 字节。

    bcrypt 只读前 72 字节，超出部分被静默截断 —— 等于密码后半段根本没参与校验。
    所以这道校验必须按字节算，这条用例就是为了防止有人把它「优化」成按字符算。
    """
    response = _register(client, password="密" * 25)

    assert response.status_code == 422
    assert "72" in response.json()["detail"][0]["msg"]


# ---------------------------------------------------------------- 登录


def test_login_with_json_returns_token(client: TestClient, user: User, password: str) -> None:
    response = client.post("/auth/login", json={"email": EMAIL, "password": password})

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    # 秒数，不是分钟：前端拿去算过期时间时单位要一致
    assert body["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


def test_login_with_form_returns_same_shape(client: TestClient, user: User, password: str) -> None:
    """/auth/token 是 OAuth2 规范的表单入口，只为了 Swagger 的 Authorize 按钮能工作。

    它的字段名是 username（规范写死的），值要填邮箱。
    """
    response = client.post(
        "/auth/token",
        data={"username": EMAIL, "password": password},
    )

    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_email_is_case_insensitive(client: TestClient, user: User, password: str) -> None:
    response = client.post("/auth/login", json={"email": EMAIL.upper(), "password": password})

    assert response.status_code == 200


def test_unknown_email_and_wrong_password_are_indistinguishable(
    client: TestClient, user: User
) -> None:
    """两条失败路径必须完全一样：状态码、文案、响应头都不能有差别。

    差异本身就是信息 —— 能区分出来，就等于给了一个「这个邮箱注册过没有」的探测器。
    顺带一提，路由里在用户不存在时也会跑一次 bcrypt，让耗时也对齐。
    """
    unknown = client.post("/auth/login", json={"email": "ghost@example.com", "password": "x"})
    wrong = client.post("/auth/login", json={"email": EMAIL, "password": "wrong-password"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()
    assert unknown.json()["detail"] == "邮箱或密码错误"
    assert unknown.headers["WWW-Authenticate"] == "Bearer"


def test_login_on_disabled_account_returns_403(client: TestClient, db: Session, user: User, password: str) -> None:
    """403 而不是 401：身份是真的，只是没有权限。

    注意这个判断放在密码校验之后 —— 否则随便输个密码就能探出「这个账号被禁用了」。
    """
    user.is_active = False
    db.commit()

    response = client.post("/auth/login", json={"email": EMAIL, "password": password})

    assert response.status_code == 403
    assert response.json()["detail"] == "账号已被禁用"


# ---------------------------------------------------------------- /auth/me 与 401 文案


def test_me_returns_current_user(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/auth/me", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["email"] == EMAIL


def test_me_rejects_garbage_token(client: TestClient) -> None:
    response = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401
    assert response.json()["detail"] == "无效的凭证"


def test_me_rejects_expired_token(client: TestClient, user: User) -> None:
    """exp 已经过去。PyJWT 抛 ExpiredSignatureError，比「无效」多一层语义。"""
    expired = create_access_token(user.id, expires_minutes=-1)

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "登录已过期，请重新登录"


def test_me_rejects_token_of_deleted_user(client: TestClient) -> None:
    """签名有效、也没过期，但库里没有这个人。

    这正是「每个请求都查一次库」的意义：token 一旦签发就无法撤销，
    只有查库才能让删号立刻生效。
    """
    ghost = create_access_token(999_999)

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {ghost}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "无效的凭证"


def test_me_on_disabled_account_returns_403(client: TestClient, db: Session, user: User) -> None:
    """拿着有效的旧 token 也不行 —— 禁用必须立刻生效。"""
    token = create_access_token(user.id)
    user.is_active = False
    db.commit()

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json()["detail"] == "账号已被禁用"
