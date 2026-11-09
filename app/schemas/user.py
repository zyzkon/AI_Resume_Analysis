from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

_BCRYPT_MAX_BYTES = 72


class _EmailNormalized(BaseModel):
    """注册与登录共用的邮箱归一化规则。

    两处规则必须永远一致：注册时把邮箱转小写再入库，登录时也必须转小写再查，
    否则 WHERE email = ? 用大小写不同的地址就匹配不上，用户明明密码正确也登不进去。
    抽成基类就是为了防止有人只改了一处。
    """

    email: EmailStr = Field(description="邮箱地址")

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        """统一转小写并去掉首尾空格。"""
        return value.strip().lower()


class UserRegister(_EmailNormalized):
    """注册请求体。

    只负责判断"这次请求带的数据是否合规"，不管数据库、不管密码怎么存。
    校验失败时 FastAPI 会自动返回 422，路由里不需要再写 if 判断。
    """

    password: str = Field(
        min_length=8,
        max_length=64,
        description="至少 8 位，最长 64 个字符",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"email": "user@example.com", "password": "Passw0rd!"}
        }
    )

    @field_validator("password")
    @classmethod
    def _check_password_bytes(cls, value: str) -> str:
        """按字节数把关，而不是字符数。

        max_length=64 限的是字符，一个汉字占 3 字节，25 个汉字就是 75 字节，
        已经超过 bcrypt 的 72 字节上限。超出的部分会被截断，等于密码后半段没生效。
        """
        if len(value.encode("utf-8")) > _BCRYPT_MAX_BYTES:
            raise ValueError(f"密码过长：按 UTF-8 编码不能超过 {_BCRYPT_MAX_BYTES} 字节")
        return value


class UserLogin(_EmailNormalized):
    """登录请求体。

    注意这里**不校验密码强度**。老用户的密码可能是早期规则下设置的，
    一旦在登录入口加了 min_length，他们就直接登不进来了。
    强度只在注册时把关，登录只校验"对不对"。
    """

    password: str = Field(min_length=1, description="登录密码")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"email": "user@example.com", "password": "Passw0rd!"}
        }
    )


class UserOut(BaseModel):
    """用户信息出参。

    白名单机制：只有这里声明的字段才会被序列化返回。
    hashed_password 不在这里，所以永远不会泄露给客户端。
    """

    id: int
    email: EmailStr
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 1,
                "email": "user@example.com",
                "is_active": True,
                "created_at": "2026-11-01T09:04:39Z",
            }
        },
    )

    @field_validator("created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        """补上 UTC 时区标记。

        SQLite 的 DateTime(timezone=True) 存不住时区，读出来是 naive datetime。
        直接序列化会得到 "2026-11-01T09:04:39"（无时区），前端 new Date() 会按
        本地时间解析，显示结果差 8 小时。这里统一标成 UTC。
        """
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Token(BaseModel):
    """登录成功响应。"""

    access_token: str = Field(description="放在 Authorization: Bearer 后面")
    token_type: str = Field(default="bearer", description="固定为 bearer")
    expires_in: int = Field(description="有效期，单位秒")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "access_token": (
                    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
                    ".eyJzdWIiOiIxIiwiZXhwIjoxNzk0MTIzNDU2fQ"
                    ".3vJ8kQ2mNpR7sT1xY6bW9cZ4dF5gH0jL-aE8uI2oK7M"
                ),
                "token_type": "bearer",
                "expires_in": 86400,
            }
        }
    )
