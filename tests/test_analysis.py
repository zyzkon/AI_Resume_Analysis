"""分析的提交、轮询、历史、走势、对比，以及 service 层的状态收敛。

两条被测链路要分清：

- **API 链路**：`client.post(...)` 走路由，`.delay()` 被替换成空实现 ——
  测试里不能真往 Redis 投消息。断的是"接口行为"：状态码、落库的初始状态、越权。
- **service 链路**：直接 `execute_analysis(db, id)`，完全绕开队列和 SessionLocal。
  这正是当初把业务从 tasks.py 抽出来的收益，否则这组用例得起 Redis 和 worker。

状态收敛（pending -> running -> done/failed）是异步系统的命门：
任何一条失败分支漏了写库，用户就会看到一条永远 `running` 的僵尸记录。
所以失败路径的用例比成功路径还多。
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.analysis import Analysis, AnalysisStatus
from app.models.resume import Resume
from app.models.user import User
from app.schemas.analysis import AnalysisResult, SectionCheck, Suggestion
from app.services.analysis import execute_analysis, mark_failed
from app.services.llm import LLMError
from app.services.prompts import PROMPT_VERSION
from app.workers.tasks import run_analysis

_SECTIONS = ("基本信息", "教育背景", "项目经历", "专业技能")


# ---------------------------------------------------------------- 造数据


def _suggestion(issue: str, advice: str = "改", priority: str = "high") -> Suggestion:
    return Suggestion(priority=priority, category="量化", issue=issue, advice=advice)


def _result(
    *,
    overall: int = 70,
    match: int = 60,
    issues: list[str] | None = None,
    sections: tuple[str, ...] = _SECTIONS,
) -> dict:
    parsed = AnalysisResult(
        overall_score=overall,
        summary="测试总评",
        completeness=[SectionCheck(name=name, present=True, comment="已包含") for name in sections],
        highlights=["有亮点"],
        weaknesses=["有短板"],
        match_score=match,
        match_reason="测试依据",
        suggestions=[_suggestion(issue) for issue in (issues or ["缺少量化数据"])],
    )
    # mode="json"：写进 JSON 列之前必须把 StrEnum / datetime 都变成基本类型
    return parsed.model_dump(mode="json")


def _add(
    db: Session,
    resume: Resume,
    *,
    status: AnalysisStatus = AnalysisStatus.DONE,
    result: dict | None = None,
    created_at: datetime | None = None,
    model_name: str = "mock",
) -> Analysis:
    kwargs: dict = {}
    if created_at is not None:
        kwargs["created_at"] = created_at

    analysis = Analysis(
        resume_id=resume.id,
        user_id=resume.user_id,
        status=status,
        result=result,
        match_score=(result or {}).get("match_score"),
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
        **kwargs,
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)
    return analysis


def _other_user_resume(db: Session) -> Resume:
    other = User(email="other@example.com", hashed_password=hash_password("other-pass-1234"))
    db.add(other)
    db.commit()
    db.refresh(other)

    resume = Resume(
        user_id=other.id,
        original_filename="theirs.pdf",
        stored_filename="theirs-stored.pdf",
        file_size=100,
        char_count=2,
        raw_text="正文",
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)
    return resume


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """把投递换成"记一笔 id"，不连 Redis。

    patch 的是任务对象上的 delay 属性 —— `analyses.py` 里
    `from app.workers.tasks import run_analysis` 拿到的是同一个对象，
    所以改在哪个命名空间导入都指向同一份。
    """
    sent: list[int] = []
    monkeypatch.setattr(run_analysis, "delay", sent.append)
    return sent


# ---------------------------------------------------------------- 提交


def test_create_analysis_returns_202_and_pending(
    client: TestClient, auth_headers: dict[str, str], resume: Resume, dispatched: list[int]
) -> None:
    response = client.post(f"/resumes/{resume.id}/analyses", headers=auth_headers, json={})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["result"] is None
    # 版本号在提交时就写死，而不是等 worker 成功后再补 ——
    # 否则失败记录会落成建表时的默认值 "v1"，一个从未存在过的版本号
    assert body["prompt_version"] == PROMPT_VERSION
    assert dispatched == [body["id"]]


def test_create_analysis_stores_job_description(
    client: TestClient, auth_headers: dict[str, str], resume: Resume, dispatched: list[int]
) -> None:
    response = client.post(
        f"/resumes/{resume.id}/analyses",
        headers=auth_headers,
        json={"job_description": "招聘 Python 后端工程师"},
    )

    assert response.json()["jd_text"] == "招聘 Python 后端工程师"


def test_create_analysis_treats_blank_jd_as_absent(
    client: TestClient, auth_headers: dict[str, str], resume: Resume, dispatched: list[int]
) -> None:
    """前端表单没填时通常提交 ""，原样存进 jd_text 会让 Prompt 多出一段空白。"""
    response = client.post(
        f"/resumes/{resume.id}/analyses",
        headers=auth_headers,
        json={"job_description": "   "},
    )

    assert response.json()["jd_text"] is None


def test_create_analysis_on_other_users_resume(
    client: TestClient, db: Session, auth_headers: dict[str, str], dispatched: list[int]
) -> None:
    theirs = _other_user_resume(db)

    response = client.post(f"/resumes/{theirs.id}/analyses", headers=auth_headers, json={})

    assert response.status_code == 404


def test_create_analysis_rejects_blank_resume_text(
    client: TestClient, db: Session, auth_headers: dict[str, str], resume: Resume, dispatched: list[int]
) -> None:
    """库里可能存着历史数据或解析后被清空过，再兜一道。

    这种请求根本不该占一条队列名额，所以在建记录之前就拒掉。
    """
    resume.raw_text = "   "
    db.commit()

    response = client.post(f"/resumes/{resume.id}/analyses", headers=auth_headers, json={})

    assert response.status_code == 400
    assert "没有可用正文" in response.json()["detail"]
    assert dispatched == []


def test_create_analysis_marks_failed_when_dispatch_fails(
    client: TestClient,
    db: Session,
    auth_headers: dict[str, str],
    resume: Resume,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """broker 挂了要返回 503，并且记录必须收敛成 failed。

    不收敛的话，用户会拿着一个 202 + pending 的 id 永远轮询下去。
    503 而不是 500：这是「队列暂时不可用」，退避重试有意义。
    """
    def _boom(_analysis_id: int) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(run_analysis, "delay", _boom)

    response = client.post(f"/resumes/{resume.id}/analyses", headers=auth_headers, json={})

    assert response.status_code == 503
    record = db.scalar(select(Analysis).where(Analysis.resume_id == resume.id))
    assert record is not None
    assert record.status == AnalysisStatus.FAILED
    assert "任务投递失败" in (record.error_message or "")


# ---------------------------------------------------------------- 查询与列表


def test_get_analysis_hides_other_users_record(
    client: TestClient, db: Session, auth_headers: dict[str, str], resume: Resume
) -> None:
    theirs = _add(db, _other_user_resume(db), result=_result())

    response = client.get(f"/analyses/{theirs.id}", headers=auth_headers)

    assert response.status_code == 404


def test_list_analyses_includes_every_status(
    client: TestClient, db: Session, auth_headers: dict[str, str], resume: Resume
) -> None:
    """失败记录也要能看到，否则用户会以为「我点了但接口没生效」。"""
    _add(db, resume, status=AnalysisStatus.PENDING, result=None)
    _add(db, resume, status=AnalysisStatus.DONE, result=_result())
    _add(db, resume, status=AnalysisStatus.FAILED, result=None)

    body = client.get(f"/resumes/{resume.id}/analyses", headers=auth_headers).json()

    assert body["total"] == 3
    statuses = {item["status"] for item in body["items"]}
    assert statuses == {"pending", "done", "failed"}


def test_list_analyses_hides_other_users_resume(
    client: TestClient, db: Session, auth_headers: dict[str, str]
) -> None:
    theirs = _other_user_resume(db)

    response = client.get(f"/resumes/{theirs.id}/analyses", headers=auth_headers)

    assert response.status_code == 404


# ---------------------------------------------------------------- 走势


def test_trend_only_done_in_ascending_order(
    client: TestClient, db: Session, auth_headers: dict[str, str], resume: Resume
) -> None:
    """只画完成的点：排队中和失败的没有分数，混进去会变成断点。

    返回按时间正序，前端直接喂给折线图不用再排一次。
    """
    older = _add(
        db,
        resume,
        result=_result(overall=60),
        created_at=datetime(2026, 1, 1, 9, 0, 0),
    )
    newer = _add(
        db,
        resume,
        result=_result(overall=80),
        created_at=datetime(2026, 1, 2, 9, 0, 0),
    )
    _add(db, resume, status=AnalysisStatus.PENDING, result=None, created_at=datetime(2026, 1, 3))
    # 状态是 failed 却带着 result：历史脏数据，靠 status 条件挡掉
    _add(db, resume, status=AnalysisStatus.FAILED, result=_result(overall=10))

    body = client.get(f"/resumes/{resume.id}/analyses/trend", headers=auth_headers).json()

    assert [point["overall_score"] for point in body["points"]] == [60, 80]
    assert body["best_analysis_id"] == newer.id
    assert body["latest_analysis_id"] == newer.id
    assert older.id not in {body["best_analysis_id"], body["latest_analysis_id"]}


def test_trend_is_empty_for_fresh_resume(
    client: TestClient, auth_headers: dict[str, str], resume: Resume
) -> None:
    body = client.get(f"/resumes/{resume.id}/analyses/trend", headers=auth_headers).json()

    assert body["points"] == []
    assert body["best_analysis_id"] is None
    assert body["latest_analysis_id"] is None


def test_trend_returns_500_for_corrupt_result(
    client: TestClient, db: Session, auth_headers: dict[str, str], resume: Resume
) -> None:
    """表里可能躺着几个月前存下的结构。一条坏数据不该让整个接口裸抛 500 堆栈，
    而应该收敛成带 id 的 500，运维一眼能定位是哪条记录。"""
    _add(db, resume, result={"overall_score": "不是数字"})

    response = client.get(f"/resumes/{resume.id}/analyses/trend", headers=auth_headers)

    assert response.status_code == 500
    assert "结果数据已损坏" in response.json()["detail"]


# ---------------------------------------------------------------- 对比


def test_compare_rejects_same_id(
    client: TestClient, db: Session, auth_headers: dict[str, str], resume: Resume
) -> None:
    done = _add(db, resume, result=_result())

    response = client.get(
        f"/resumes/{resume.id}/analyses/compare",
        params={"base_id": done.id, "target_id": done.id},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert "不能相同" in response.json()["detail"]


def test_compare_rejects_unfinished_analysis(
    client: TestClient, db: Session, auth_headers: dict[str, str], resume: Resume
) -> None:
    """409 而不是 400：请求本身没错，是资源当前状态不允许这个操作。

    前端拿到 409 应该提示「等分析跑完」，而不是「参数填错了」。
    """
    done = _add(db, resume, result=_result())
    pending = _add(db, resume, status=AnalysisStatus.PENDING, result=None)

    response = client.get(
        f"/resumes/{resume.id}/analyses/compare",
        params={"base_id": done.id, "target_id": pending.id},
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert "尚未完成" in response.json()["detail"]


def test_compare_404_when_analysis_belongs_to_another_resume(
    client: TestClient, db: Session, auth_headers: dict[str, str], resume: Resume, user: User
) -> None:
    """两条记录都要同时满足 id 属于这份简历、这份简历属于我。

    只写 id.in_() 的话，拿别人的分析 id 也能查出来。
    """
    base = _add(db, resume, result=_result())

    other_resume = Resume(
        user_id=user.id,
        original_filename="second.pdf",
        stored_filename="second-stored.pdf",
        file_size=10,
        char_count=2,
        raw_text="正文",
    )
    db.add(other_resume)
    db.commit()
    db.refresh(other_resume)
    elsewhere = _add(db, other_resume, result=_result())

    response = client.get(
        f"/resumes/{resume.id}/analyses/compare",
        params={"base_id": base.id, "target_id": elsewhere.id},
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_compare_returns_diff(
    client: TestClient, db: Session, auth_headers: dict[str, str], resume: Resume
) -> None:
    base = _add(
        db,
        resume,
        result=_result(overall=60, match=55, issues=["缺少量化数据", "格式混乱"]),
        created_at=datetime(2026, 1, 1),
    )
    target = _add(
        db,
        resume,
        result=_result(
            overall=72,
            match=68,
            # 第一条是「改了措辞的同一个问题」，第二条是全新问题
            issues=["缺少量化数据，建议补充", "缺少自我评价"],
        ),
        created_at=datetime(2026, 1, 2),
    )

    body = client.get(
        f"/resumes/{resume.id}/analyses/compare",
        params={"base_id": base.id, "target_id": target.id},
        headers=auth_headers,
    ).json()

    assert body["stats"]["overall_delta"] == 12
    assert body["stats"]["match_delta"] == 13
    assert body["stats"]["persisted_issues"] == 1
    assert body["stats"]["resolved_issues"] == 1
    assert body["stats"]["new_issues"] == 1
    assert "提升 12 分" in body["stats"]["verdict"]

    # 排序是刻意的：仍存在（没落地的旧建议）最该被看到，已解决放最后给正反馈
    assert [item["change"] for item in body["suggestions"]] == ["persisted", "new", "resolved"]
    assert body["suggestions"][0]["similarity"] >= 0.55
    assert body["base"]["id"] == base.id
    assert body["target"]["suggestion_count"] == 2


# ---------------------------------------------------------------- service 层


def test_execute_analysis_marks_done(db: Session, resume: Resume) -> None:
    """走的是 mock provider，但整条编排是真的：标 running -> 调模型 -> 写 result。

    正文自己查库（`analysis.resume.raw_text`），不随消息传递 ——
    两万字塞进 Redis 消息体既浪费又会被 broker 的大小限制卡住。
    """
    analysis = _add(db, resume, status=AnalysisStatus.PENDING, result=None)

    assert execute_analysis(db, analysis.id) == "done"

    db.refresh(analysis)
    assert analysis.status == AnalysisStatus.DONE
    assert analysis.result is not None
    assert analysis.match_score == analysis.result["match_score"]
    assert analysis.model_name == "mock"
    # started_at 和 created_at 之差就是排队耗时，两个都必须落
    assert analysis.started_at is not None
    assert analysis.finished_at is not None


def test_execute_analysis_skips_finished_record(db: Session, resume: Resume) -> None:
    """acks_late 打开后，worker 崩溃会让同一条消息被重投。

    幂等靠「已是终态就跳过」来挡，不然一份简历会被反复计费分析。
    """
    analysis = _add(db, resume, result=_result(overall=88))

    assert execute_analysis(db, analysis.id) == "already_done"

    db.refresh(analysis)
    assert analysis.result["overall_score"] == 88


def test_execute_analysis_handles_missing_record(db: Session) -> None:
    """记录可能在建任务之后被删了（删简历会级联删分析）。"""
    assert execute_analysis(db, 999_999) == "not_found"


def test_execute_analysis_fails_on_blank_text(db: Session, resume: Resume) -> None:
    analysis = _add(db, resume, status=AnalysisStatus.PENDING, result=None)
    resume.raw_text = "   "
    db.commit()

    assert execute_analysis(db, analysis.id) == "failed"

    db.refresh(analysis)
    assert analysis.status == AnalysisStatus.FAILED
    assert "正文为空" in (analysis.error_message or "")


def test_execute_analysis_marks_failed_on_llm_error(
    db: Session, resume: Resume, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型超时是**预期内的业务失败**，不能 raise。

    raise 出去会被 Celery 记成任务异常、触发监控告警；
    而正确做法是收敛成 failed + error_message，让用户看到原因。
    """
    def _raise(_text, _jd=None):
        raise LLMError("模型连续 3 次超时")

    monkeypatch.setattr("app.services.analysis.analyze_resume", _raise)
    analysis = _add(db, resume, status=AnalysisStatus.PENDING, result=None)

    assert execute_analysis(db, analysis.id) == "failed"

    db.refresh(analysis)
    assert analysis.status == AnalysisStatus.FAILED
    assert analysis.error_message == "模型连续 3 次超时"
    assert analysis.finished_at is not None


def test_execute_analysis_truncates_long_error_message(
    db: Session, resume: Resume, monkeypatch: pytest.MonkeyPatch
) -> None:
    """错误信息可能带着几百行堆栈，截 500 字足够定位，也避免把列表接口撑大。"""
    def _raise(_text, _jd=None):
        raise LLMError("x" * 2000)

    monkeypatch.setattr("app.services.analysis.analyze_resume", _raise)
    analysis = _add(db, resume, status=AnalysisStatus.PENDING, result=None)

    execute_analysis(db, analysis.id)

    db.refresh(analysis)
    assert len(analysis.error_message or "") == 500


def test_mark_failed_ignores_missing_record(db: Session) -> None:
    """标记失败本身也可能失败（记录已被删）。这里只能靠日志留痕，不能抛。"""
    mark_failed(db, 999_999, "随便什么原因")
