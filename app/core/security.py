"""密码哈希与 JWT 签发/校验。

这一层是纯函数：不碰数据库，不碰 HTTP，不知道 FastAPI 的存在。
好处是能脱离整个应用单独测试，也方便以后被 worker 或脚本复用。

刻意不用 passlib：它在 Python 3.13 上与 bcrypt 4.x/5.x 冲突，
会抛 AttributeError: module 'bcrypt' has no attribute '__about__'。
直接调 bcrypt 库只有三行代码，没有必要引入那层封装。
"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings

# bcrypt 算法本身只读取密码的前 72 字节。
# bcrypt 5.x 遇到更长的输入会直接抛 ValueError（而不是像旧版那样静默截断），
# 所以这里主动截断，保证长密码不会变成 500。
_BCRYPT_MAX_BYTES = 72


class TokenError(Exception):
    """token 无法通过校验（签名不对、格式不对、已过期）。

    单独定义一个异常，是为了让上层（api/deps.py）只需要捕获这一种，
    而不用把 PyJWT 的异常体系泄露到路由层。
    """


def _to_bytes(password: str) -> bytes:
    """按 UTF-8 编码并截断到 bcrypt 能吃下的长度。"""
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """生成 bcrypt 哈希，可直接存进 users.hashed_password。

    每次调用都会生成新的随机盐（gensalt），所以同一个密码两次哈希结果不同，
    这是正常现象——校验时靠 checkpw 从哈希串里把盐读出来。
    """
    return bcrypt.hashpw(_to_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验密码。

    两种情况都返回 False 而不是抛异常：
    1. 密码不匹配
    2. 数据库里的哈希串格式非法（比如被手工改坏了）

    第 2 种如果不拦，bcrypt 会抛 ValueError，最终变成 500 错误，
    而用户看到的应该是"邮箱或密码错误"。
    """
    try:
        return bcrypt.checkpw(_to_bytes(plain_password), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(
    subject: str | int,
    expires_minutes: int | None = None,
) -> str:
    """签发 access token。

    subject 统一转成字符串放进 sub：JWT 规范（RFC 7519）要求 sub 是字符串，
    塞 int 进去部分库会拒绝解码，是常见的跨语言兼容坑。
    """
    now = datetime.now(timezone.utc)
    minutes = expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    payload = {
        "sub": str(subject),
        "iat": now,                       # 签发时间
        "exp": now + timedelta(minutes=minutes),  # 过期时间，PyJWT 会自动转成时间戳
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解码并校验 token，返回 payload。

    algorithms 必须显式写成列表。如果传 None 或写错，PyJWT 会接受任何算法，
    攻击者可以把 alg 改成 none 伪造 token——这是 JWT 最经典的漏洞。
    """
    try:
        return jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("无效的凭证") from exc
