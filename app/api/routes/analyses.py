"""简历分析的提交、查询与历史列表。

这一层只做三件事：找数据、调服务层、写数据库。
Prompt 怎么拼、模型怎么调、输出怎么校验，全部在 services/llm.py 里，
这一层不该出现任何 if settings.LLM_PROVIDER 之类的判断。

当前是同步实现：请求打进来 -> 调模型 -> 写库 -> 返回，用户要等满整个分析时间。
阶段 6 会改成"创建 pending 记录立刻返回，Celery worker 后台跑"，
届时接口形状不变，只是 status 会先返回 pending / running。
"""

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
from app.services.llm import LLMError, analyze_resume
from app.services.prompts import PROMPT_VERSION

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
        status=analysis.status,
        match_score=analysis.match_score,
        overall_score=result.get("overall_score"),
        summary=result.get("summary"),
        created_at=analysis.created_at,
    )


@router.post(
    "/{resume_id}/analyses",
    response_model=AnalysisOut,
    status_code=status.HTTP_201_CREATED,
    summary="提交分析",
    description="同步执行：调模型、写库、返回完整结果。失败时返回 502，失败记录仍保留。",
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
        # 但库里可能存着历史数据，或解析后正文被清空过，这里再兜一道
        raise HTTPException(status_code=400, detail="这份简历没有可用正文，无法分析")

    jd_text = payload.job_description if payload else None

    # 先落一条 running 记录，再调模型。
    # 这一次 commit 的意义：如果调模型的过程中进程被杀（Ctrl+C、OOM、断网卡死），
    # 数据库里会留下一条永远停在 running 的记录 —— 一眼就能看出是哪条出了问题。
    # 如果等全部跑完再一次性写入，进程一死就什么都不剩，等于这次请求从没发生过。
    analysis = Analysis(
        resume_id=resume.id,
        user_id=current_user.id,
        jd_text=jd_text,
        status=AnalysisStatus.RUNNING,
        # 在这里就写下版本号，而不是等成功后再补。
        # 否则失败记录会落成建表时的 server_default "v1"，
        # 一个项目里从未存在过的版本号，看数据时会被误导
        prompt_version=PROMPT_VERSION,
        started_at=datetime.now(timezone.utc),
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)  # created_at 是数据库生成的，不 refresh 拿到的是 None

    try:
        outcome = analyze_resume(resume.raw_text, jd_text)
    except LLMError as exc:
        # 失败也要落库：用户需要知道"这次试过、失败了、原因是什么"，
        # 否则列表里看不到记录，会以为是接口没生效
        analysis.status = AnalysisStatus.FAILED
        analysis.error_message = str(exc)[:500]
        analysis.finished_at = datetime.now(timezone.utc)
        db.commit()
        # 502 而不是 500：错在模型服务（网关之后的上游），不在我们自己
        raise HTTPException(status_code=502, detail=f"分析失败：{exc}") from exc

    # mode="json" 不能省：普通 model_dump() 会留下 StrEnum、
    # 以及将来可能出现的 datetime，MySQL 的 JSON 列写不进去
    analysis.result = outcome.result.model_dump(mode="json")
    analysis.match_score = outcome.result.match_score
    analysis.model_name = outcome.model
    analysis.prompt_version = outcome.prompt_version
    analysis.status = AnalysisStatus.DONE
    analysis.finished_at = datetime.now(timezone.utc)
    db.commit()

    # 从库里读回来再返回，而不是直接返回内存里的对象。
    # 这样 response_model 校验的是 MySQL 实际存下的那份 JSON，
    # 序列化踩坑（比如某字段存成了字符串）当场就会暴露，而不是等前端发现
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
