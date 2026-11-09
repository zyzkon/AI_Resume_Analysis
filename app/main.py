"""应用入口：创建 FastAPI 实例、挂中间件、注册路由。

这个文件只做"拼装"，不放任何业务逻辑。
每条路由的具体实现都在 api/routes/ 下，各自持有自己的 router。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.openapi import tags_metadata
from app.api.routes import analyses, auth, resumes
from app.core.config import settings
from app.schemas.system import HealthOut


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时确保上传目录存在。

    之所以不用 @app.on_event("startup")：那个写法已废弃，
    而且它没法方便地表达"启动和关闭成对执行"。lifespan 里
    yield 之前是启动逻辑，之后是关闭逻辑（这里没有需要清理的资源）。
    """
    settings.ensure_dirs()
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

app.add_middleware(
    CORSMiddleware,
    # 仅限开发环境。上线前必须换成具体的前端域名：
    # "*" 配合 allow_credentials=True 会被浏览器直接拒绝，而且等于允许任意站点调用接口
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
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
def health() -> HealthOut:
    return HealthOut(
        status="ok",
        version=settings.APP_VERSION,
        debug=settings.DEBUG,
    )
