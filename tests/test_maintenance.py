"""僵尸任务清理的用例。

被测对象是 services/maintenance.py 里的纯业务函数，不需要 Redis、不需要 worker ——
这正是当初把逻辑从 tasks.py 抽出来的收益。

阈值全部由用例自己传，不读 settings：既能精确构造"刚好超时"的边界，
也不会因为哪天有人调大 LLM_TIMEOUT 就让这些用例莫名其妙变红。
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Analysis, AnalysisStatus, Resume, User
from app.services.maintenance import ReapResult, reap_stuck

RUNNING_SECONDS = 600
PENDING_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make(
    db: Session,
    user: User,
    resume: Resume,
    *,
    status: AnalysisStatus,
    created_ago: int | None = None,
    started_ago: int | None = None,
) -> Analysis:
    """造一条指定状态、指定时间点的分析记录。

    created_at / started_at 显式赋值，覆盖掉列的 server_default ——
    要测"15 分钟前就在跑"这种事，只能由用例自己把时间拨回去。
    """
    now = _now()
    item = Analysis(
        resume_id=resume.id,
        user_id=user.id,
        status=status,
        created_at=now - timedelta(seconds=created_ago) if created_ago is not None else now,
        started_at=now - timedelta(seconds=started_ago) if started_ago is not None else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _reap(db: Session) -> ReapResult:
    return reap_stuck(
        db,
        running_seconds=RUNNING_SECONDS,
        pending_seconds=PENDING_SECONDS,
    )


def test_running_over_limit_is_failed(db: Session, user: User, resume: Resume) -> None:
    item = _make(db, user, resume, status=AnalysisStatus.RUNNING, started_ago=RUNNING_SECONDS + 60)

    result = _reap(db)

    db.refresh(item)
    assert result.running == 1
    assert item.status == AnalysisStatus.FAILED
    # 原因必须写清楚，否则用户看到一条没有理由的失败记录
    assert item.error_message is not None
    assert "运行超过" in item.error_message
    assert item.finished_at is not None


def test_running_within_limit_is_untouched(db: Session, user: User, resume: Resume) -> None:
    """还在阈值内就一点都不能碰。

    这是最容易写错的方向：清理器误杀正常任务，比留着僵尸记录更糟 ——
    用户会看到状态从 running 跳到 failed，然后又跳回 done。
    """
    item = _make(db, user, resume, status=AnalysisStatus.RUNNING, started_ago=60)

    result = _reap(db)

    db.refresh(item)
    assert result.running == 0
    assert item.status == AnalysisStatus.RUNNING
    assert item.error_message is None
    assert item.finished_at is None


def test_running_without_started_at_falls_back_to_created_at(
    db: Session, user: User, resume: Resume
) -> None:
    """started_at 为 NULL 的脏数据也要能清掉。

    直接写 `started_at < cutoff` 的话，NULL 参与比较永远为假，
    这类记录会永远漏网 —— 而它恰恰是最该被清掉的那种。
    """
    item = _make(
        db,
        user,
        resume,
        status=AnalysisStatus.RUNNING,
        created_ago=RUNNING_SECONDS + 60,
        started_ago=None,
    )

    result = _reap(db)

    db.refresh(item)
    assert result.running == 1
    assert item.status == AnalysisStatus.FAILED


def test_pending_over_limit_is_failed(db: Session, user: User, resume: Resume) -> None:
    item = _make(db, user, resume, status=AnalysisStatus.PENDING, created_ago=PENDING_SECONDS + 60)

    result = _reap(db)

    db.refresh(item)
    assert result.pending == 1
    assert item.status == AnalysisStatus.FAILED
    assert item.error_message is not None
    assert "排队超过" in item.error_message


def test_pending_within_limit_is_untouched(db: Session, user: User, resume: Resume) -> None:
    """排队一小时以内不算死：worker 可能只是暂时没起，或者队列里压着别的长任务。"""
    item = _make(db, user, resume, status=AnalysisStatus.PENDING, created_ago=60)

    result = _reap(db)

    db.refresh(item)
    assert result.pending == 0
    assert item.status == AnalysisStatus.PENDING


@pytest.mark.parametrize("status", [AnalysisStatus.DONE, AnalysisStatus.FAILED])
def test_finished_records_are_never_touched(
    db: Session, user: User, resume: Resume, status: AnalysisStatus
) -> None:
    """终态记录哪怕放了一年也不该被改。

    WHERE 里那两个 `status == ...` 就是在防这件事：
    只按时间判断的话，一份很久以前完成的分析会被改成 failed。
    """
    item = _make(db, user, resume, status=status, created_ago=365 * 24 * 3600)

    result = _reap(db)

    db.refresh(item)
    assert result.total == 0
    assert item.status == status


def test_counts_are_reported_separately(db: Session, user: User, resume: Resume) -> None:
    """running 和 pending 要分开计数。

    两个数字指向完全不同的故障：running 多说明 worker 被杀，
    pending 多说明根本没有 worker 在消费队列。合成一个数字就看不出是哪边出事。
    """
    for _ in range(2):
        _make(db, user, resume, status=AnalysisStatus.RUNNING, started_ago=RUNNING_SECONDS + 60)
    _make(db, user, resume, status=AnalysisStatus.PENDING, created_ago=PENDING_SECONDS + 60)
    # 一条不该动的，用来确认计数不是"把表里所有行都算上"
    _make(db, user, resume, status=AnalysisStatus.RUNNING, started_ago=10)

    result = _reap(db)

    assert result.running == 2
    assert result.pending == 1
    assert result.total == 3


def test_second_run_is_noop(db: Session, user: User, resume: Resume) -> None:
    """清理要幂等：beat 每 5 分钟跑一次，同一条记录不能每轮都被改一遍。

    第一次已经把它推进 failed 了，第二次的 WHERE 里 `status == running` 不成立。
    """
    _make(db, user, resume, status=AnalysisStatus.RUNNING, started_ago=RUNNING_SECONDS + 60)

    first = _reap(db)
    second = _reap(db)

    assert first.total == 1
    assert second.total == 0
