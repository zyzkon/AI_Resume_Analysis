"""跨域用例：钉死「只有白名单里的来源才会被浏览器放行」。

先说清楚这里测的是什么。CORS 是**浏览器**的策略，服务端只是决定回不回那几个
`Access-Control-*` 响应头。所以：

- 能测的：「针对这个来源，头回得对不对」
- 不能测的：「攻击者能不能调接口」—— curl、Postman、任何非浏览器客户端
  根本不看 CORS。这一点由 `test_blocked_origin_still_gets_response` 正面钉死，
  免得有人把收紧 CORS 当成鉴权手段

`preflight` 那几条对应真实浏览器行为：带 `Authorization` 头或
`Content-Type: application/json` 的请求不是「简单请求」，浏览器会先发一趟
`OPTIONS` 问「我能这么发吗」，答错了正式请求根本不会发出去。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import create_access_token
from app.models.user import User

CORS_HEADER = "access-control-allow-origin"


def _settings(**overrides: object) -> Settings:
    """造一份不读 .env 的配置，只用于验证校验器。

    `_env_file=None` 跳过磁盘上的 .env，否则开发机上的真实配置会盖掉用例意图。
    环境变量仍然生效，但显式传参优先级最高，所以这里传的值总能赢。
    """
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        SECRET_KEY="test-secret-key-0123456789abcdefgh",
        **overrides,
    )


# ---- 简单请求：白名单回显 ----


@pytest.mark.parametrize("key", ["allowed", "allowed_alt"])
def test_each_configured_origin_is_echoed(
    client: TestClient, cors_origins: dict[str, str], key: str
) -> None:
    """两个来源都要通 —— 顺带证明逗号分隔的配置真的被拆成了列表。

    只测一个的话，把 `split(",")` 写成 `split(";")` 这种错误照样全绿。
    """
    origin = cors_origins[key]

    resp = client.get("/health", headers={"Origin": origin})

    assert resp.status_code == 200
    assert resp.headers[CORS_HEADER] == origin
    # 按来源变化的响应必须带 Vary，否则中间缓存可能把 A 站拿到的响应发给 B 站
    assert "origin" in resp.headers["vary"].lower()


def test_blocked_origin_still_gets_response(
    client: TestClient, cors_origins: dict[str, str]
) -> None:
    """不在名单上的来源：服务端照常执行，只是**不回** CORS 头。

    这条是故意留着提醒人的 —— 没有 `Access-Control-Allow-Origin` 不等于请求被拒，
    接口该跑的逻辑一行没少跑。浏览器拿到响应后因为缺这个头而拒绝把它交给页面 JS，
    仅此而已。所以 CORS 永远不能当访问控制用，鉴权还得靠 token。
    """
    resp = client.get("/health", headers={"Origin": cors_origins["blocked"]})

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert CORS_HEADER not in resp.headers


def test_no_origin_header_gets_no_cors_header(client: TestClient) -> None:
    """同源请求和 curl 都不带 Origin，此时不该凭空多出 CORS 头。"""
    resp = client.get("/health")

    assert resp.status_code == 200
    assert CORS_HEADER not in resp.headers


# ---- 预检请求 ----


def test_preflight_allows_post_with_authorization(
    client: TestClient, cors_origins: dict[str, str]
) -> None:
    """最典型的一趟预检：前端要带 `Authorization` 发 POST。"""
    origin = cors_origins["allowed"]

    resp = client.options(
        "/resumes",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert resp.status_code == 200
    assert resp.headers[CORS_HEADER] == origin
    assert "POST" in resp.headers["access-control-allow-methods"]
    # Starlette 会把 CORS 安全头并进来，所以这里除了自配的两个还有其他值
    assert "authorization" in resp.headers["access-control-allow-headers"].lower()
    assert resp.headers["access-control-max-age"] == "600"


def test_preflight_rejects_unimplemented_method(
    client: TestClient, cors_origins: dict[str, str]
) -> None:
    """`allow_methods` 没写 "*" 的效果：PUT 直接答 400。

    这正是收紧 `allow_methods` 的意义 —— 浏览器在预检阶段就知道这个动作不被支持，
    不会把请求发出来。写 "*" 等于对所有方法点头。
    """
    resp = client.options(
        "/resumes",
        headers={
            "Origin": cors_origins["allowed"],
            "Access-Control-Request-Method": "PUT",
        },
    )

    assert resp.status_code == 400
    assert "method" in resp.text


def test_preflight_rejects_unknown_header(
    client: TestClient, cors_origins: dict[str, str]
) -> None:
    """自定义头不在 `allow_headers` 里，预检就过不去。"""
    resp = client.options(
        "/resumes",
        headers={
            "Origin": cors_origins["allowed"],
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-requested-with",
        },
    )

    assert resp.status_code == 400
    assert "headers" in resp.text


def test_preflight_rejects_unlisted_origin(
    client: TestClient, cors_origins: dict[str, str]
) -> None:
    """跨域预检不只查方法，第一件事是查来源。"""
    resp = client.options(
        "/resumes",
        headers={
            "Origin": cors_origins["blocked"],
            "Access-Control-Request-Method": "POST",
        },
    )

    assert resp.status_code == 400
    assert "origin" in resp.text
    assert CORS_HEADER not in resp.headers


# ---- 与认证的关系 ----


def test_bearer_token_works_with_allow_credentials_false(
    client: TestClient, user: User, cors_origins: dict[str, str]
) -> None:
    """`allow_credentials=False` 不影响 Bearer token。

    容易搞混的一点：CORS 的 "credentials" 指的是 **cookie、浏览器自带的 Basic 认证
    和 TLS 客户端证书**。代码里手动塞进 `Authorization` 头的 token 只是一个普通请求头，
    受 `allow_headers` 管辖。所以这个项目不需要（也不该）打开 credentials 开关，
    除非哪天改成用 cookie 存 token。
    """
    origin = cors_origins["allowed"]

    resp = client.get(
        "/auth/me",
        headers={
            "Origin": origin,
            "Authorization": f"Bearer {create_access_token(user.id)}",
        },
    )

    assert resp.status_code == 200
    assert resp.headers[CORS_HEADER] == origin
    # 没开 credentials，就不该出现这个头；出现了说明有人误开了开关
    assert "access-control-allow-credentials" not in resp.headers


# ---- 配置校验器 ----


def test_trailing_slash_and_spaces_are_normalized() -> None:
    """结尾斜杠和空格是无害笔误，直接帮忙修掉，不用报错。"""
    config = _settings(CORS_ORIGINS="  http://a.example.com/ , , http://b.example.com:8080  ")

    assert config.cors_origins == ["http://a.example.com", "http://b.example.com:8080"]


def test_empty_means_no_cross_origin() -> None:
    """空值 = 不放行任何跨域，这是前端与接口同域部署时的正确形态。"""
    assert _settings(CORS_ORIGINS="").cors_origins == []


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("localhost:5173", "缺协议头"),
        ("http://a.example.com/api", "带路径"),
    ],
)
def test_malformed_origin_is_rejected(value: str, reason: str) -> None:
    """这两种写法都不会报错地「静默不生效」，所以必须在启动时炸掉。"""
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        _settings(CORS_ORIGINS=value)


def test_wildcard_is_rejected_in_production() -> None:
    """DEBUG=false 还写 "*"，等于允许任何站点读接口响应。"""
    with pytest.raises(ValidationError, match=r"不能含 \*"):
        _settings(DEBUG=False, CORS_ORIGINS="*")


def test_wildcard_is_allowed_in_debug() -> None:
    """开发期确实有人图省事写 "*"，留个口子但不设成默认值。"""
    assert _settings(DEBUG=True, CORS_ORIGINS="*").cors_origins == ["*"]


def test_default_only_allows_5173() -> None:
    """默认值只放行本机 5173，且不含 "*"。

    这条钉的是「别为了本地方便随手往默认值里加端口」——默认值是所有人 clone 之后
    实际拿到的配置，改它等于替所有人做决定。要加端口应该改自己的 `.env`。
    """
    default = Settings.model_fields["CORS_ORIGINS"].default

    assert isinstance(default, str)
    origins = _settings(CORS_ORIGINS=default).cors_origins
    assert origins == ["http://localhost:5173", "http://127.0.0.1:5173"]
