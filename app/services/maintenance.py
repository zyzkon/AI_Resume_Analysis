"""僵尸任务清理：把永远不会再更新的 pending / running 记录收敛成 failed。

## 为什么需要它

`execute_analysis` 里已经把每条失败路径都收敛了，但有三条它管不到：

1. **硬超时**（`task_time_limit`）由 worker 直接杀进程，python 来不及执行任何收尾代码
2. **worker 被 OOM / 断电 / kill -9 打断**，同上下场
3. **broker 丢了消息**（Redis 没开持久化又重启过），记录停在 pending，再也不会有 worker 领它

前两条还能靠 `visibility_timeout` 到期重投救一次，但如果消息已经丢了、
或者重投之后 worker 又挂了，就再没有任何人去改这一行。
用户看到的是一个永远转圈的 id —— 他既不知道是排队、是卡住、还是已经失败。

## 这一层只做"收敛"，不做"重试"

理由和 `tasks.py` 里 `max_retries=0` 一样：后台偷偷重跑会烧 token，
而且用户已经等了十几分钟，再让他等第二次体验更差。
清理只负责把状态推到终态、写清原因，让前端能给出确定结论，
由用户自己决定要不要重新提交。

## 阈值为什么必须远大于硬超时

清理的任务是"确认已死"，不是"催活"。如果阈值离硬超时太近，
一个本来就快跑完的任务会被误标 failed，随后又被自己写成 done ——
用户会看到状态从 running 跳到 failed 再跳到 done，比卡住更让人困惑。
所以阈值取 `HARD_TIME_LIMIT + 120`，留出「进程被杀的延迟 + 重投后重跑」的窗口。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import Update, func, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models import Analysis, AnalysisStatus

logger = logging.getLogger(__name__)


def _rowcount(db: Session, stmt: Update) -> int:
    """执行 UPDATE 并返回受影响行数。

    `Session.execute` 的静态返回类型是 `Result`，但跑 DML 时实际拿到的是
    `CursorResult` —— `rowcount` 只在后者身上。mypy 看不到这个运行时事实，
    所以用一次 cast 把类型收窄，调用处就不必每句都写。
    """
    return cast("CursorResult[Any]", db.execute(stmt)).rowcount


@dataclass(frozen=True)
class ReapResult:
    """一次清理的战果。分开计数是因为两者含义不同：

    running 多，说明 worker 稳定性有问题（被 OOM 杀、机器重启）；
    pending 多，说明 worker 根本没在跑，或者队列积压到没人消费。
    混成一个数字就没法从日志里区分这两种故障。
    """

    running: int
    pending: int

    @property
    def total(self) -> int:
        return self.running + self.pending


def _now() -> datetime:
    """统一 UTC，和 services/analysis.py 里的时间口径一致。"""
    return datetime.now(timezone.utc)


def reap_stuck(
    db: Session,
    *,
    running_seconds: int,
    pending_seconds: int,
) -> ReapResult:
    """把超过时限仍未进入终态的记录标成 failed。

    时间阈值由调用方传入，不在这里读 settings：
    这样测试可以传 0 秒直接构造"全都超时"的场景，不用真等。

    两个 WHERE 都带 `status == ...`，不是只靠时间判断 ——
    完成得慢的任务（比如排了 20 分钟才跑、跑了 1 分钟就成功）不该被误伤。
    它现在是 done，第一个条件就不成立。
    """
    now = _now()
    running_cutoff = now - timedelta(seconds=running_seconds)
    pending_cutoff = now - timedelta(seconds=pending_seconds)

    running = _rowcount(
        db,
        update(Analysis)
        .where(
            Analysis.status == AnalysisStatus.RUNNING,
            # 正常情况下 started_at 一定有值；万一历史数据缺这一列，
            # 退到 created_at 判断，否则 NULL 参与比较永远为假，这类记录会漏掉
            func.coalesce(Analysis.started_at, Analysis.created_at) < running_cutoff,
        )
        .values(
            status=AnalysisStatus.FAILED,
            error_message=f"任务运行超过 {running_seconds} 秒仍未结束，已由清理任务标记为失败",
            finished_at=now,
        )
        # 绕过 ORM 的会话内同步：这条 UPDATE 可能命中当前 Session 里没加载的行，
        # 让 SQLAlchemy 去逐条比对本会话的对象纯属浪费
        .execution_options(synchronize_session=False),
    )

    pending = _rowcount(
        db,
        update(Analysis)
        .where(
            Analysis.status == AnalysisStatus.PENDING,
            Analysis.created_at < pending_cutoff,
        )
        .values(
            status=AnalysisStatus.FAILED,
            error_message=(
                f"任务排队超过 {pending_seconds} 秒仍未被执行，"
                "已由清理任务标记为失败（worker 可能没有运行）"
            ),
            finished_at=now,
        )
        .execution_options(synchronize_session=False),
    )

    # 两条 UPDATE 放在同一个事务里提交：要么都生效，要么都不生效。
    # 分开 commit 只会在第二次失败时留下"清了一半"的中间状态
    db.commit()

    result = ReapResult(running=running, pending=pending)
    if result.total:
        logger.warning(
            "清理僵尸分析：running=%s（超 %s 秒），pending=%s（超 %s 秒）",
            running,
            running_seconds,
            pending,
            pending_seconds,
        )
    return result
