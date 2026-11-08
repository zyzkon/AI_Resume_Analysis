"""简历分析的提交、查询与历史列表。

这一层只做三件事：找数据、调服务层、写数据库。
Prompt 怎么拼、模型怎么调、输出怎么校验，全部在 services/llm.py 里，
这一层不该出现任何 if settings.LLM_PROVIDER 之类的判断。

当前是异步实现：接口建一条 pending 记录、把 id 丢进 Celery 队列，
立刻返回 202；真正的模型调用在 app/workers/tasks.py 里跑，
前端拿 id 轮询 GET /analyses/{id} 看 status 从 pending -> running -> done/failed。
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.analysis import Analysis, AnalysisStatus
from app.models.resume import Resume
from app.models.user import User
from app.schemas.analysis import (
    AnalysisCreate,
    AnalysisListItem,
    AnalysisListOut,
    AnalysisOut,
)
from app.services.prompts import PROMPT_VERSION
from app.workers.tasks import run_analysis

logger = logging.getLogger(__name__)

# 两个前缀没法共用一个 APIRouter：
# 提交和列表挂在某份简历下面（/resumes/{id}/analyses），
# 查单条结果按分析 ID 查（/analyses/{id}），后者前端拿到 id 之后最方便。
router = APIRouter(prefix="/resumes", tags=["分析"])
analysis_router = APIRouter(prefix="/analyses", tags=["分析"])


def _get_own_resume(db: Session, resume_id: int, user: User) -> Resume:
    """取当前用户名下的简历，取不到一律 404。

    单独抽出来是因为提交和列表两个端点都要做这件事。
    这里绝不能写成 db.get(Resume, resume_id)：那样只按主键查，
    A 用户把 URL 里的 id 改成 B 用户的简历就能基于别人的简历发起分析。
    """
    resume = db.scalar(
        select(Resume).where(
            Resume.id == resume_id,
            Resume.user_id == user.id,
        )
    )
    if resume is None:
        raise HTTPException(status_code=404, detail="简历不存在")
    return resume


def _to_list_item(analysis: Analysis) -> AnalysisListItem:
    """把 ORM 对象摊平成列表项。

    为什么不给 AnalysisListItem 加 from_attributes 让 FastAPI 自动转？
    因为 overall_score 和 summary 并不在表里，它们埋在 result 这个 JSON 列内部。
    自动转换只能读属性，读不到 JSON 里面的键，只能在这里显式取一次。
    """
    result = analysis.result or {}
    return AnalysisListItem(
        id=analysis.id,
        resume_id=analysis.resume_id,
        # 列上声明的是 Mapped[str]（String(20)），取出来是裸字符串；
        # 而 schema 里 status 是 AnalysisStatus 枚举。Pydantic 本来也会做这层转换，
        # 这里显式转一次是为了让"数据库里的字符串"和"接口上的枚举"在类型上对齐，
        # 顺便把脏数据的报错点提到构造这一行，而不是藏在序列化过程里
        status=AnalysisStatus(analysis.status),
        match_score=analysis.match_score,
        overall_score=result.get("overall_score"),
        summary=result.get("summary"),
        created_at=analysis.created_at,
    )


@router.post(
    "/{resume_id}/analyses",
    response_model=AnalysisOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="提交分析（异步）",
    description=(
        "建一条 pending 记录后立刻返回，实际的模型调用在后台 worker 里执行。\n\n"
        "拿返回的 id 轮询 `GET /analyses/{id}`，status 会依次变成 "
        "`pending` -> `running` -> `done`（或 `failed`，此时 error_message 有原因）。"
    ),
)
def create_analysis(
    resume_id: int,
    payload: AnalysisCreate | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Analysis:
    resume = _get_own_resume(db, resume_id, current_user)

    if not (resume.raw_text or "").strip():
        # 阶段 3 的解析器对扫描件会抛错，理论上进不来；
        # 但库里可能存着历史数据，或解析后正文被清空过，这里再兜一道。
        # 放在建记录之前：这种请求根本不该占一条队列名额
        raise HTTPException(status_code=400, detail="这份简历没有可用正文，无法分析")

    jd_text = payload.job_description if payload else None

    # 先落库、再投递。顺序反过来的话，worker 可能在记录还不存在时就开始跑，
    # db.get 拿不到东西，任务直接 return "not_found"，用户看到一条永远 pending 的分析
    analysis = Analysis(
        resume_id=resume.id,
        user_id=current_user.id,
        jd_text=jd_text,
        status=AnalysisStatus.PENDING,
        # 版本号在这里就写。等到 worker 成功后再补的话，
        # 失败记录会落成建表时的 server_default "v1"，一个项目里从未存在过的版本号
        prompt_version=PROMPT_VERSION,
        # started_at 故意不写：它表示"worker 真正开始跑的时刻"，
        # 由任务自己落，这样 created_at 与它之差就是排队耗时
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)  # id 和 created_at 都是数据库生成的

    try:
        run_analysis.delay(analysis.id)
    except Exception as exc:  # noqa: BLE001
        # 这里 catch 得宽是有意的：Redis 挂了、序列化失败、broker 拒绝连接，
        # 异常类型随底层库版本变化。共同后果只有一个 —— 任务没进队列。
        # 不兜住的话，用户会看到 202 + pending 的记录，然后永远等不到结果
        logger.exception("任务投递失败 analysis_id=%s", analysis.id)
        analysis.status = AnalysisStatus.FAILED
        analysis.error_message = f"任务投递失败：{exc}"[:500]
        analysis.finished_at = datetime.now(timezone.utc)
        db.commit()
        # 503 而不是 500：是"队列这个依赖暂时不可用"，重试有意义，客户端该退避后再来
        raise HTTPException(
            status_code=503,
            detail="分析队列暂不可用，请稍后重试",
        ) from exc

    # 再读一次：worker 可能已经领走任务并把状态改成 running 了，
    # 直接返回能让前端少一轮无意义的轮询
    db.refresh(analysis)
    return analysis


@analysis_router.get(
    "/{analysis_id}",
    response_model=AnalysisOut,
    summary="查询分析结果",
)
def get_analysis(
    analysis_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Analysis:
    analysis = db.scalar(
        select(Analysis).where(
            Analysis.id == analysis_id,
            Analysis.user_id == current_user.id,
        )
    )
    if analysis is None:
        raise HTTPException(status_code=404, detail="分析记录不存在")
    return analysis


@router.get(
    "/{resume_id}/analyses",
    response_model=AnalysisListOut,
    summary="某份简历的分析历史",
)
def list_analyses(
    resume_id: int,
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    size: int = Query(20, ge=1, le=100, description="每页条数，最大 100"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnalysisListOut:
    # 先确认简历归属：不确认的话，随便填个 id 就能知道"这个 id 有没有分析记录"
    _get_own_resume(db, resume_id, current_user)

    # 条件里同时带 resume_id 和 user_id。前者是业务过滤，
    # 后者是数据隔离 —— 万一有人绕过简历校验，这一层还能兜住
    conditions = (Analysis.resume_id == resume_id, Analysis.user_id == current_user.id)

    total = db.scalar(select(func.count()).select_from(Analysis).where(*conditions)) or 0

    # id 作为次级排序键：created_at 只到秒，同一秒内连点两次会排序不稳，
    # 分页时可能重复或漏行
    rows = db.scalars(
        select(Analysis)
        .where(*conditions)
        .order_by(Analysis.created_at.desc(), Analysis.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()

    return AnalysisListOut(
        total=total,
        page=page,
        size=size,
        items=[_to_list_item(row) for row in rows],
    )
