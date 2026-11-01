from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """所有运行时配置的唯一来源。

    字段分三类：
    1. 有默认值的 -> 环境变量缺失时用默认值
    2. 必填的（无默认值） -> 缺失时导入即抛错，避免带着错误配置上线
    3. 派生值 -> 由其他字段计算得出，不写进 .env
    """

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    APP_NAME: str = "Resume Analyzer API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = True

    SECRET_KEY: str = Field(
        ...,
        min_length=32,
        description="JWT 签名密钥，至少 32 字符",
    )
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    DATABASE_URL: str = "sqlite:///./app.db"
    DB_ECHO: bool = False

    UPLOAD_DIR: Path = Path("uploads")
    MAX_UPLOAD_MB: int = 5

    REDIS_URL: str = "redis://localhost:6379/0"

    LLM_PROVIDER: str = "mock"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-chat"
    LLM_TIMEOUT: int = 120
    LLM_MAX_RETRIES: int = 2

    @field_validator("DATABASE_URL", mode="after")
    @classmethod
    def _normalize_sqlite_path(cls, value: str) -> str:
        """把 sqlite 的相对路径改写成基于项目根目录的绝对路径。

        否则同一份配置在 api 进程和 celery worker 里会因 cwd 不同而指向两个库。
        """
        prefix = "sqlite:///"
        if not value.startswith(prefix):
            return value
        raw = value[len(prefix) :]
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            return value
        absolute = (BASE_DIR / raw).resolve()
        return f"{prefix}{absolute.as_posix()}"

    @field_validator("UPLOAD_DIR", mode="after")
    @classmethod
    def _normalize_upload_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    def ensure_dirs(self) -> None:
        """应用启动时调用，保证上传目录存在。"""
        self.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
