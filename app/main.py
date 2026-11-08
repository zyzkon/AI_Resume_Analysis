from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import analyses, auth, resumes
from app.core.config import settings

tags_metadata = [
    {"name": "系统", "description": "健康检查等基础接口"},
    {"name": "用户", "description": "注册、登录、获取当前用户"},
    {"name": "简历", "description": "上传简历并解析纯文本"},
    {"name": "分析", "description": "提交 AI 分析任务、查询结果与历史"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "基于大模型的简历分析服务。\n\n"
        "流程：**注册 -> 登录拿 token -> 上传简历 -> 提交分析任务 -> 轮询结果**。\n"
        "点右上角 Authorize 填入登录返回的 access_token 即可调试受保护接口。"
    ),
    openapi_tags=tags_metadata,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
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


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
        "debug": settings.DEBUG,
    }
