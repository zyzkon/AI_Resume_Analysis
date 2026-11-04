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

    DATABASE_URL: str = "mysql+pymysql://resume:resume_pass_2026@127.0.0.1:3306/resume_db?charset=utf8mb4"
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
    def _check_database_url(cls, value: str) -> str:
        """启动时就校验驱动前缀，而不是等第一个请求打进来才发现配错。

        必须是 mysql+pymysql:// 形式：
        - mysql     是 SQLAlchemy 的方言名，决定用哪套 SQL 生成规则
        - +pymysql  是具体驱动，决定用哪个 Python 库发连接
        少了驱动部分，SQLAlchemy 会去找 MySQLdb（需要编译，Windows 上装不上）。
        """
        if not value.startswith(("mysql+pymysql://", "mysql+mysqldb://")):
            raise ValueError(
                "DATABASE_URL 必须以 mysql+pymysql:// 开头，当前为: "
                f"{value.split('://')[0] if '://' in value else value}"
            )
        return value

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
