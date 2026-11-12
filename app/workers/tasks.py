"""Celery 任务层：只负责"进程生命周期"，业务逻辑在 services/ 下。

这里剩下的几件事，特征都是"只有跑在 Celery 里才存在"：
1. 开 Session / 关 Session —— 路由有 Depends 帮忙，任务里没有
2. 捕获 SoftTimeLimitExceeded —— 这是 Celery 的信号，业务层不该认识它
3. 未预期异常记日志后重新抛出 —— 让 Celery 把任务标成 FAILURE，监控才能看见
4. 定时任务注册 —— beat 调度表引用的任务名必须在这里存在，否则发出去也没人认

业务逻辑（幂等判断、标 running、调模型、写回结果、清理僵尸记录）全在 services 里。
这样写脚本批量重跑、或者写单元测试时，不必启动 Redis 和 worker。

和路由层最重要的区别：Session 必须自己开关。
路由里有 FastAPI 帮忙收尾，任务里忘了 close 就是连接泄漏 ——
worker 是常驻进程，泄漏不会自己恢复，跑一夜就把 MySQL 的
max_connections 占满。
"""

import logging

from celery.exceptions import SoftTimeLimitExceeded

from app.db.session import SessionLocal
from app.services.analysis import execute_analysis, mark_failed
from app.services.maintenance import reap_stuck
from app.workers.celery_app import (
    SOFT_TIME_LIMIT,
    STUCK_PENDING_SECONDS,
    STUCK_RUNNING_SECONDS,
    celery_app,
)

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="analysis.run",
    # 不自动重试：llm.py 内部已经按 LLM_MAX_RETRIES 重试过一轮，
    # 这里再叠一层，最坏情况是 3×3=9 次请求、用户等 8 分钟才看到失败。
    # 真要"任务级重试"，应该由用户主动点重试（新建一条记录），
    # 而不是让系统在后台偷偷烧 token
    max_retries=0,
    # 不把结果留在 Redis 里等别人取：状态本来就在 MySQL 的 status 列上，
    # Redis 里再存一份只会带来"两边不一致该信谁"的问题
    ignore_result=True,
)
def run_analysis(self, analysis_id: int) -> str:
    """队列入口。返回值只用于日志和排查，业务状态一律以 MySQL 的 status 为准。"""
    db = SessionLocal()
    try:
        return execute_analysis(db, analysis_id)

    except SoftTimeLimitExceeded:
        # 超时信号抛出时，会话里可能还挂着未提交的中间状态，先回滚。
        # 这条路径能安全 commit 是因为信号只是抛异常，进程还活着；
        # 如果这里也卡住，Celery 的 hard limit 会直接杀进程，记录留在 running
        db.rollback()
        # self.soft_time_limit 来自 @task 配置，独立调用任务时可能为 None
        limit = self.soft_time_limit or SOFT_TIME_LIMIT
        try:
            mark_failed(db, analysis_id, f"任务超时（超过 {limit} 秒）")
        except Exception:  # noqa: BLE001
            # 连标记状态都失败了（数据库不可用），只能靠日志留痕
            logger.exception("标记超时状态时出错，analysis_id=%s", analysis_id)
        logger.error("分析 %s 超时被杀", analysis_id)
        return "timeout"

    except Exception as exc:  # noqa: BLE001
        # 没预料到的异常（数据库断了、代码有 bug）。
        # 先把记录收敛成 failed，再把异常抛出去让 Celery 记 FAILURE：
        # 记录是给用户看的，异常是给开发看的，两者都不能少
        logger.exception("分析 %s 出现未预期异常", analysis_id)
        db.rollback()
        try:
            mark_failed(db, analysis_id, f"内部错误：{exc}")
        except Exception:  # noqa: BLE001
            logger.exception("标记失败状态时再次出错，analysis_id=%s", analysis_id)
        raise

    finally:
        # worker 是常驻进程，不 close 就是慢性连接泄漏
        db.close()


@celery_app.task(
    name="analysis.reap_stuck",
    ignore_result=True,
)
def reap_stuck_analyses() -> str:
    """定时清理僵尸记录，由 beat 每 5 分钟投递一次（调度表在 celery_app.beat_schedule）。

    不加 bind=True：这个任务不需要 self —— 它不读 soft_time_limit、不重试，
    自身耗时也就一条 UPDATE 的功夫，用不上超时信号。

    也不吞异常：数据库连不上就让它抛，Celery 记成 FAILURE，
    下一轮 beat 自然还会再试。这里"静默成功"比"报错"危险得多 ——
    清理器坏了却没人知道，僵尸记录会无声地堆积起来。
    """
    db = SessionLocal()
    try:
        result = reap_stuck(
            db,
            running_seconds=STUCK_RUNNING_SECONDS,
            pending_seconds=STUCK_PENDING_SECONDS,
        )
        return f"running={result.running} pending={result.pending}"
    finally:
        db.close()
