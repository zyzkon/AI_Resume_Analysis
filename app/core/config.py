from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator
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

    # ---- 大模型 ----
    # mock 不联网、不花钱、结果确定；openai 走 OpenAI 兼容接口
    # （DeepSeek、通义、Kimi、本地 vLLM 都兼容这一套，只改 BASE_URL 和 MODEL）
    LLM_PROVIDER: Literal["mock", "openai"] = "mock"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-chat"
    LLM_TIMEOUT: float = 90.0
    LLM_MAX_RETRIES: int = 2
    # 温度压到 0.2：简历打分需要可复现，同样的简历两次分析不该差出 20 分
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_TOKENS: int = 2048
    # 送进模型前的硬上限。20000 字已经是很长的简历了，
    # 再长基本是 PDF 解析时把页眉页脚、二维码说明都吸进来了，截断反而更准
    LLM_MAX_INPUT_CHARS: int = 20000

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

    @field_validator("LLM_API_KEY", mode="after")
    @classmethod
    def _require_key_for_real_provider(cls, value: str, info: ValidationInfo) -> str:
        """配了真实通道却没给 key，启动就报错。

        等第一个分析请求打进来才报 401，那时候用户已经等了半分钟，
        而且排查时容易怀疑到网络、额度、模型名上去。
        """
        provider = info.data.get("LLM_PROVIDER", "mock")
        if provider != "mock" and not value.strip():
            raise ValueError(f"LLM_PROVIDER={provider} 时必须配置 LLM_API_KEY")
        return value.strip()

    @property
    def llm_is_mock(self) -> bool:
        return self.LLM_PROVIDER == "mock"

    def ensure_dirs(self) -> None:
        """应用启动时调用，保证上传目录存在。"""
        self.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    # SECRET_KEY 是必填项（Field(...) 无默认值），值来自 .env 或环境变量。
    # mypy 看不到运行时才加载的 .env，会以为这个参数漏传了，属于误报
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
