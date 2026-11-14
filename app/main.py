"""应用入口：创建 FastAPI 实例、挂中间件、注册路由。

这个文件只做"拼装"，不放任何业务逻辑。
每条路由的具体实现都在 api/routes/ 下，各自持有自己的 router。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.openapi import tags_metadata
from app.api.routes import analyses, auth, resumes
from app.core.concurrency import ConcurrencyGate
from app.core.config import settings
from app.db.session import MAX_DB_CONNECTIONS, align_thread_pool
from app.schemas.system import HealthOut

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时确保上传目录存在，并把并发上限对齐。

    之所以不用 @app.on_event("startup")：那个写法已废弃，
    而且它没法方便地表达"启动和关闭成对执行"。lifespan 里
    yield 之前是启动逻辑，之后是关闭逻辑（这里没有需要清理的资源）。
    """
    settings.ensure_dirs()
    # uvicorn 只给自己的 logger 挂 handler，应用里的 logger.info 默认无处可去。
    # 补一次 basicConfig，下面这行和 services 里的告警才会真的进容器日志。
    # 判断 handlers 是否存在，是为了不覆盖 pytest 或上层已经配好的日志
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s:     [%(name)s] %(message)s")
    # 同步路由的线程池容量 = 并发闸门。原因见 app/core/concurrency.py：
    # 要限的是同时在处理的请求数，线程只是它的下游
    align_thread_pool(settings.MAX_CONCURRENT_REQUESTS)
    logger.info(
        "并发闸门 %d ｜ 线程池 %d ｜ 连接池 %d（DB_POOL_SIZE %d + MAX_OVERFLOW %d）",
        settings.MAX_CONCURRENT_REQUESTS,
        settings.MAX_CONCURRENT_REQUESTS,
        MAX_DB_CONNECTIONS,
        settings.DB_POOL_SIZE,
        settings.DB_MAX_OVERFLOW,
    )
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "基于大模型的简历分析服务。\n\n"
        "## 快速上手\n\n"
        "1. `POST /auth/register` 注册\n"
        "2. `POST /auth/token`（或 `/auth/login`）拿到 `access_token`\n"
        "3. 点右上角 **Authorize** 填入 token —— 之后所有受保护接口会自动带上请求头，"
        "不用每次手工拼 `Authorization: Bearer ...`\n"
        "4. `POST /resumes` 上传 PDF 或 DOCX\n"
        "5. `POST /resumes/{id}/analyses` 提交分析，拿到 `analysis_id`\n"
        "6. 轮询 `GET /analyses/{analysis_id}`，直到 `status` 变成 `done`（或 `failed`）\n\n"
        "## 关于异步\n\n"
        "分析**不是同步返回的**。提交接口建一条 `pending` 记录就立刻回 202，"
        "真正的模型调用在 Celery worker 里执行。\n\n"
        "这意味着：**只启动 API 而不启动 worker 时，分析记录会永远停在 `pending`**。"
        "这是异步系统的固有特性，不是 bug。\n\n"
        "## 错误码约定\n\n"
        "| 码 | 含义 |\n"
        "| --- | --- |\n"
        "| 400 | 请求内容不合法（简历无正文、拿同一条分析做对比） |\n"
        "| 401 | 未认证、token 无效或已过期 |\n"
        "| 403 | 身份有效但账号被禁用 |\n"
        "| 404 | 资源不存在，**或者存在但不属于当前用户** |\n"
        "| 409 | 资源状态冲突（邮箱已注册、参与对比的分析未跑完） |\n"
        "| 413 | 文件超过大小上限 |\n"
        "| 415 | 文件类型不支持 |\n"
        "| 422 | 请求参数格式错误（字段缺失、类型不对、数值越界） |\n"
        "| 500 | 服务端内部错误 |\n"
        "| 503 | 任务队列不可用，稍后重试即可 |\n\n"
        "> 404 与 403 的选择是刻意的：越权访问统一返回 404，"
        "因为 403 等于承认「这个 id 确实存在，只是你没权限」，"
        "那本身就是一个可以用来枚举资源的信息。"
    ),
    openapi_tags=tags_metadata,
    contact={
        "name": "zyzkon",
        "url": "https://github.com/zyzkon/AI_Resume_Analysis",
    },
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# 闸门要在 CORS 之前 add：后添加的中间件更靠外，所以下面那段 CORS 会在
# 闸门之外先处理掉预检请求 —— OPTIONS 不该占用业务名额
app.add_middleware(ConcurrencyGate, limit=settings.MAX_CONCURRENT_REQUESTS)

app.add_middleware(
    CORSMiddleware,
    # 来源列在 .env 的 CORS_ORIGINS 里，逗号分隔；默认只有本机的开发端口。
    # 绝不写 "*"：那等于允许任何站点的 JS 读这个接口的响应，
    # 而且会掩盖配置错误 —— 上线换成具体域名的那天，
    # 才发现前端用的 origin 拼错、或者带了 www.
    allow_origins=settings.cors_origins,
    # 这里保持 False 是对的，而且和「用 Bearer token」并不矛盾。
    # CORS 里的 "credentials" 只涵盖 cookie、浏览器自带的 Basic 认证、TLS 客户端证书；
    # 代码里手动塞进 Authorization 头的 token 属于「普通请求头」，
    # 由 allow_headers 管辖，跟这个开关无关。
    # 哪天改成用 cookie 存 token，才需要把它翻成 True —— 同时 allow_origins 必须
    # 全是具体域名（"*" 与 True 在浏览器规范里互斥）
    allow_credentials=False,
    # 只列接口真正用到的方法。写 "*" 会被 Starlette 展开成 ALL_METHODS，
    # 等于对外声明「支持 PUT / PATCH」，而这两个方法一个都没有实现
    allow_methods=["GET", "POST", "DELETE"],
    # 同理，只放行自己会用到的头。注意 Content-Type: multipart/form-data 属于
    # CORS 安全头，本来就不触发预检；列进来是为了 application/json 的那些请求
    allow_headers=["Authorization", "Content-Type"],
    # 预检结果在浏览器里缓存 10 分钟，省掉每个非简单请求前的一趟 OPTIONS 往返
    max_age=600,
)

# 路由必须在这里注册，app 对象才算"认识"这些路径。
# 每个模块自己持有 router，main 只负责拼装，模块之间互不导入。
app.include_router(auth.router)
app.include_router(resumes.router)
app.include_router(analyses.router)
app.include_router(analyses.analysis_router)


@app.get(
    "/health",
    response_model=HealthOut,
    tags=["系统"],
    summary="健康检查",
    description=(
        "不需要认证。只返回进程是否活着，**不检查 MySQL / Redis** —— "
        "依赖抖一下就摘掉整个服务，会把小故障放大成全局不可用。"
    ),
)
async def health() -> HealthOut:
    """刻意写成 async def，而不是普通的 def。

    同步路由会被丢进线程池排队，满负荷时探活请求排在业务请求后面，
    3 秒超时一到容器就被标成 unhealthy —— 压测时真实发生过。
    这个函数不碰数据库、没有阻塞调用，留在事件循环里直接返回，
    于是"服务忙"和"服务死"能被区分开。
    """
    return HealthOut(
        status="ok",
        version=settings.APP_VERSION,
        debug=settings.DEBUG,
    )
