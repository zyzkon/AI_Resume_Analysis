"""认证路由：注册、登录、获取当前用户。

四个端点分成两组：
- /register、/login、/token 是公开的，谁都能调
- /me 受保护，靠 deps.get_current_user 挂上鉴权

为什么登录要提供两个地址（/login 和 /token）？
纯历史原因，但两边都有实际用途：
- /token 接收 application/x-www-form-urlencoded，是 OAuth2 规范规定的格式，
  Swagger UI 的 Authorize 按钮只会按这个格式发请求，少了它文档里就没法调试
- /login 接收 JSON，是给前端用的。浏览器前端发 JSON 更自然，也能带上更丰富的字段
两者最终都走同一个 _authenticate，逻辑只有一份。
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.openapi import AUTH_REQUIRED, CONFLICT_EMAIL, FORBIDDEN, INVALID_CREDENTIALS
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import Token, UserLogin, UserOut, UserRegister

router = APIRouter(prefix="/auth", tags=["用户"])


# 一个"根本没有对应账号"时用来陪跑的哈希值。
# 作用是抵御时序攻击：bcrypt 校验要花约 100ms，如果"用户不存在"直接返回、
# "密码错误"才跑 bcrypt，两者响应时间差了几个数量级，
# 攻击者能靠计时枚举出哪些邮箱已注册。所以两条路径都老老实实算一次。
_DUMMY_HASH = hash_password("timing-attack-placeholder")


def _invalid_credentials() -> HTTPException:
    """登录失败的统一响应。

    不区分"邮箱不存在"和"密码错误"，文案和状态码完全一致。
    否则攻击者拿一个邮箱试一次，就能判断这个邮箱是否注册过。
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="邮箱或密码错误",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authenticate(db: Session, email: str, password: str) -> User:
    """校验邮箱密码，通过则返回 User，不通过直接抛异常。"""
    # email 再归一化一次：登录入口的 schema 已经转小写，
    # 但 /token 用的是表单，OAuth2PasswordRequestForm 不做任何归一化，走到这里的是原始字符串。
    user = db.scalar(select(User).where(User.email == email.strip().lower()))

    if user is None:
        verify_password(password, _DUMMY_HASH)  # 只为消耗同样的时间，结果丢弃
        raise _invalid_credentials()

    if not verify_password(password, user.hashed_password):
        raise _invalid_credentials()

    # 放在密码校验之后：否则别人随便输个密码就能探出"这个账号被禁用了"。
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用",
        )

    return user


def _issue_token(user: User) -> Token:
    """签发 token 并包装成响应体。"""
    return Token(
        access_token=create_access_token(user.id),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="注册",
    description=(
        "注册成功后直接返回用户信息，**不自动登录、不下发 token**，需要再调一次登录接口。\n\n"
        "邮箱重复由数据库唯一索引判定 —— 不是先查后插，那样在「查完还没插」的间隙里"
        "两个并发请求会同时通过检查。冲突时返回 409。\n\n"
        "密码按 **UTF-8 字节数**限制在 72 以内（不是字符数）：bcrypt 只处理前 72 字节，"
        "超出的部分会被静默截断，等于密码后半段没生效。"
    ),
    responses={409: CONFLICT_EMAIL},
)
def register(payload: UserRegister, db: Session = Depends(get_db)) -> User:
    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)

    try:
        db.commit()
    except IntegrityError as exc:
        # 为什么不先 SELECT 查一下重不重？
        # 因为"查完再插"中间有窗口期：两个请求同时查到"不存在"，然后都插入。
        # 唯一索引是数据库层原子的最终裁决，只有它能真正保证不重复。
        # 先查一次只能让正常情况快一点，不能替代这里。
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该邮箱已被注册",
        ) from exc

    # commit 后主键和 created_at 还不在对象上：这两个值都由数据库生成
    # （AUTO_INCREMENT 和 DEFAULT CURRENT_TIMESTAMP），MySQL 没有 RETURNING，
    # 只能再查一次把它们取回来，否则序列化时 created_at 是 None。
    db.refresh(user)
    return user


@router.post(
    "/token",
    response_model=Token,
    summary="登录（表单，供 Swagger 使用）",
    description=(
        "接收 `application/x-www-form-urlencoded`，**`username` 字段要填邮箱** —— "
        "这是 OAuth2 规范规定的字段名，改不了。\n\n"
        "和 `POST /auth/login` 返回的 token 完全等价，只是请求格式不同："
        "这个入口是为了让 Swagger 的 **Authorize** 按钮能工作，"
        "点它的时候浏览器发的是表单而不是 JSON。"
    ),
    responses={401: INVALID_CREDENTIALS, 403: FORBIDDEN},
)
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> Token:
    user = _authenticate(db, form_data.username, form_data.password)
    return _issue_token(user)


@router.post(
    "/login",
    response_model=Token,
    summary="登录（JSON，供前端使用）",
    description=(
        "接收 JSON，给前端用的入口。\n\n"
        "邮箱不存在、密码错误这两种情况返回**完全相同**的 401 和文案，"
        "响应耗时也刻意对齐（用户不存在时照样跑一次 bcrypt），"
        "目的是不泄露「这个邮箱有没有注册过」。\n\n"
        "登录**不校验密码强度**：老用户的密码可能是早期规则下设的，"
        "在登录口加长度限制会让他们直接登不进来。强度只在注册时把关。"
    ),
    responses={401: INVALID_CREDENTIALS, 403: FORBIDDEN},
)
def login(payload: UserLogin, db: Session = Depends(get_db)) -> Token:
    user = _authenticate(db, payload.email, payload.password)
    return _issue_token(user)


@router.get(
    "/me",
    response_model=UserOut,
    summary="获取当前登录用户",
    description=(
        "把请求头里的 token 解析成用户对象。\n\n"
        "前端可以用它校验本地存的 token 还有效没有：拿到 401 就该清掉本地凭证、"
        "跳回登录页，而不是等某个业务接口莫名其妙失败。\n\n"
        "每次请求都会查一次数据库（走主键索引，成本极低）。"
        "不这么做的话，token 一旦签发就无法撤销 —— 用户被禁用、账号被删，"
        "只要旧 token 没过期就还能继续用。"
    ),
    responses=AUTH_REQUIRED,
)
def read_me(current_user: User = Depends(get_current_user)) -> User:
    # 不需要再查库：get_current_user 已经从数据库把整行取出来了。
    # 这里也不用判断 current_user 是否为空，拿不到用户它早就抛 401 了。
    return current_user
