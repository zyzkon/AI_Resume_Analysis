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

    # ---- 连接池 ----
    # DB_POOL_SIZE + DB_MAX_OVERFLOW 是单个进程能同时持有的 MySQL 连接数上限。
    # 它必须 **大于** MAX_CONCURRENT_REQUESTS，原因不是"多留点余量"这么随意：
    # FastAPI 的同步依赖和同步路由是分两次进线程池的，两次之间数据库连接
    # 仍然held着不归还，所以在途请求（占着连接）可以多于正在跑的线程数。
    # 2026-11 压测就是在这里塌的，详见 docs/load-test.md
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 20
    # 池耗尽时最多等多久。正常情况下永远不该触发（在途请求数被下面的闸门限住），
    # 保留它是为了不变量被破坏时快速失败：等 5 秒报错，好过让用户对着
    # 转圈等 30 秒还以为是网络问题
    DB_POOL_TIMEOUT: float = 5.0

    # ---- 并发闸门 ----
    # 在 ASGI 层挡住超出的请求，让它们在事件循环里排队，而不是进到线程池里
    # 互相堵死。这个数同时决定同步路由的线程池容量，所以它是「这台进程
    # 同时处理多少个请求」的唯一旋钮。调大要连 CPU 一起看：
    # 请求是 I/O 等待（数据库）还是 CPU 计算（JSON 解析）决定了有效值
    MAX_CONCURRENT_REQUESTS: int = 30

    # ---- 跨域 ----
    # 逗号分隔的完整 origin 列表。默认只放行本机 5173 端口（Vite / Vue / React 的默认端口）。
    # 为什么 localhost 和 127.0.0.1 都写上：它们是**两个不同的 origin**，
    # 浏览器按字符串精确比对，少一个就会出现「同事那边能跑、我这边被拦」。
    # 换成别的端口，或者上线换域名，改这一行即可；留空表示不允许任何跨域
    # （前端与接口同域部署时用空值）
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

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

    @field_validator("CORS_ORIGINS", mode="after")
    @classmethod
    def _normalize_cors_origins(cls, value: str, info: ValidationInfo) -> str:
        """把 `a, b/, c` 归一成 `a,b,c`，顺手挡掉三种最常见的写错。

        为什么不直接用 `list[str]`：pydantic-settings 读 list 时按 JSON 解析，
        `.env` 里得写成 `CORS_ORIGINS=["http://a","http://b"]`，引号和括号都要，
        手写容易漏。逗号分隔的字符串更好写，在这里转成列表。

        三种写错都必须当场拦下，因为它们的表现都是「配置看着没问题，
        浏览器就是不让你请求」，排查时最容易被怀疑到别的方向去：

        1. 缺协议头 —— `localhost:5173` 永远不会等于浏览器发来的 `http://localhost:5173`
        2. 带路径 —— 浏览器发来的 Origin 只有 `协议://域名[:端口]`，写 `http://a.com/api` 匹配不到
        3. 结尾斜杠 —— `http://a.com/` 与 `http://a.com` 是**两个不同字符串**
        """
        items: list[str] = []
        for raw in value.split(","):
            # 结尾斜杠直接削掉，而不是报错：这是无害的笔误，帮用户修好就行
            item = raw.strip().rstrip("/")
            if not item:
                continue
            if item != "*":
                if "://" not in item:
                    raise ValueError(
                        f"CORS_ORIGINS 里的每一项都要带协议头（http:// 或 https://），当前为: {item}"
                    )
                if "/" in item.split("://", 1)[1]:
                    raise ValueError(
                        f"CORS_ORIGINS 不能带路径，浏览器发来的 Origin 只有 协议://域名[:端口]，当前为: {item}"
                    )
            items.append(item)

        # "*" 在生产环境等于「任何站点都能读这个接口的响应」，只允许开发期图省事
        if "*" in items and not info.data.get("DEBUG", True):
            raise ValueError(
                "DEBUG=false 时 CORS_ORIGINS 不能含 *，请列出具体的前端域名（例如 https://resume.example.com）"
            )
        return ",".join(items)

    @property
    def cors_origins(self) -> list[str]:
        """给 `CORSMiddleware(allow_origins=...)` 用的列表形式。

        空字符串 -> 空列表 -> 中间件不放行任何跨域来源（同源部署的正确形态）。
        """
        return [item for item in self.CORS_ORIGINS.split(",") if item]

    @property
    def db_max_connections(self) -> int:
        """单个进程最多持有的 MySQL 连接数。

        部署多副本时要拿 MySQL 的 `max_connections` 反算：
        `max_connections / (副本数 + worker 数)` 才是每个进程能拿到的份额。
        默认 40 在 151 个连接下刚好放得下 3 个进程。
        """
        return self.DB_POOL_SIZE + self.DB_MAX_OVERFLOW

    @field_validator("MAX_CONCURRENT_REQUESTS", mode="after")
    @classmethod
    def _check_gate(cls, value: int, info: ValidationInfo) -> int:
        """闸门不能比连接池上限还宽，否则闸门形同虚设。

        这条校验存在的意义：两个数分居两处（一个在这里，一个在 SQLAlchemy
        的参数里），改了一个忘另一个的话，故障要等到压测或线上满负荷才
        暴露，表现是「平时好好的，一忙就 500」。
        """
        if value < 1:
            raise ValueError("MAX_CONCURRENT_REQUESTS 至少为 1")
        pool = info.data.get("DB_POOL_SIZE", 0) + info.data.get("DB_MAX_OVERFLOW", 0)
        if pool and value > pool:
            raise ValueError(
                f"MAX_CONCURRENT_REQUESTS={value} 超过了连接池上限 {pool}"
                f"（DB_POOL_SIZE + DB_MAX_OVERFLOW），请调大连接池或调小闸门"
            )
        return value

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
