"""分析任务的业务编排：取记录 -> 标记 running -> 调模型 -> 写回 done/failed。

这一层刻意不 import 任何 Celery 的东西。它不知道自己是"被队列里的任务触发"
还是"被一个脚本直接调用"，也不关心 Session 是谁开的、谁来关。

这样做的直接收益：
1. 单元测试可以直接 execute_analysis(db, 9)，不用起 Redis、不用起 worker
2. 批量重跑历史分析写个 for 循环就够，不用往队列里灌 100 条消息
3. 将来把 Celery 换成 RQ / Dramatiq，这个文件一行都不用改

与之相对，app/workers/tasks.py 只保留"进程生命周期"那部分职责：
开 Session、捕获 Celery 的超时信号、关 Session。
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Analysis, AnalysisStatus
from app.services.llm import LLMError, analyze_resume

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """统一用 UTC，和模型里其他时间字段对齐。"""
    return datetime.now(timezone.utc)


def mark_failed(db: Session, analysis_id: int, reason: str) -> None:
    """把记录收敛成 failed。

    所有失败分支都必须走这里，不允许某个 except 里只 log 一下就 return ——
    那样记录会一直停在 running，前端轮询永远等不到结果，
    用户只能猜"是不是我网断了"。

    参数收的是 id 而不是对象：超时那条路径在信号抛出后重新拿不到原来的
    对象引用，统一按 id 查一次，调用方不用关心对象还活着没有。
    """
    analysis = db.get(Analysis, analysis_id)
    if analysis is None:
        # 记录在建任务之后被删了（用户删简历会级联删分析），没什么可标记的
        logger.warning("分析记录 %s 不存在，无法标记失败状态", analysis_id)
        return
    analysis.status = AnalysisStatus.FAILED
    # 数据库列是 Text，但错误信息可能带着几百行堆栈；
    # 截 500 字足够定位问题，也避免一条错误把整个列表接口撑大
    analysis.error_message = (reason or "未知错误")[:500]
    analysis.finished_at = _now()
    db.commit()


def execute_analysis(db: Session, analysis_id: int) -> str:
    """跑一次完整的分析并写库。返回值只用于日志，业务状态以 status 列为准。

    幂等性说明：Celery 的 acks_late 打开后，worker 崩溃会让同一条消息被重新投递，
    这个函数可能对同一个 id 执行两次。靠"已是终态就跳过"来挡；
    而分析本身没有副作用（不扣钱、不发通知），万一真跑了两遍，
    后一次覆盖前一次，结果仍然自洽。
    """
    analysis = db.get(Analysis, analysis_id)
    if analysis is None:
        logger.warning("分析记录 %s 不存在，任务跳过", analysis_id)
        return "not_found"

    if analysis.status == AnalysisStatus.DONE:
        logger.info("分析记录 %s 已完成，跳过重复执行", analysis_id)
        return "already_done"

    # 标记 running 并落库，前端轮询立刻能看到"在跑"。
    # started_at 在这里写而不是建记录时写 —— 两者之差就是排队耗时，
    # 这个指标能直接回答"是不是该加 worker 了"
    analysis.status = AnalysisStatus.RUNNING
    analysis.started_at = _now()
    # 重跑时清掉上一次的错误信息，否则会出现"done 但还挂着错误原因"
    analysis.error_message = None
    db.commit()

    # 正文自己查库，不随消息传递。
    # 一份简历正文可能两万字，塞进 Redis 消息体既浪费又会被 broker 的大小限制卡住
    resume_text = (analysis.resume.raw_text or "").strip()
    if not resume_text:
        mark_failed(db, analysis_id, "简历正文为空，无法分析")
        return "failed"

    logger.info(
        "开始分析 analysis_id=%s，正文 %s 字，是否带 JD=%s",
        analysis_id,
        len(resume_text),
        bool(analysis.jd_text),
    )

    try:
        outcome = analyze_resume(resume_text, analysis.jd_text)
    except LLMError as exc:
        # 预期内的失败：模型超时、输出不合规、鉴权失败。
        # 属于业务结果，不是系统异常，所以吞掉不 raise，
        # 避免 Celery 把它算成任务失败、触发监控告警
        logger.warning("分析 %s 失败：%s", analysis_id, exc)
        mark_failed(db, analysis_id, str(exc))
        return "failed"

    # mode="json"：普通 model_dump() 会留下 StrEnum 和 datetime，
    # 写不进 MySQL 的 JSON 列
    analysis.result = outcome.result.model_dump(mode="json")
    analysis.match_score = outcome.result.match_score
    analysis.model_name = outcome.model
    analysis.prompt_version = outcome.prompt_version
    analysis.status = AnalysisStatus.DONE
    analysis.finished_at = _now()
    db.commit()

    logger.info(
        "分析 %s 完成：总分 %s，匹配度 %s，模型 %s，耗时 %s ms",
        analysis_id,
        outcome.result.overall_score,
        outcome.result.match_score,
        outcome.model,
        outcome.latency_ms,
    )
    return "done"
