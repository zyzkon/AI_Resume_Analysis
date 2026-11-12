"""差异计算的纯函数。

这个模块当初就是为了好测才写成纯函数的：不连数据库、不 import FastAPI、
不碰 Celery，输入两个 AnalysisResult、输出一组 delta。所以这里连 fixture 都不用，
直接构造对象调函数。

重点在两处业务判断：
1. 相似度阈值 0.55 —— 调高会把「改了措辞的同一个问题」误判成一删一增，
   调低会把两个不同问题合成一条
2. 建议的排序 —— persisted 在前（上次的建议没落地，是用户下一步最该动手的），
   resolved 在后（那是给正反馈的，不该占视觉首位）
"""

from app.schemas.analysis import AnalysisResult, SectionCheck, Suggestion
from app.services.comparison import (
    MATCH_THRESHOLD,
    compare_results,
    diff_sections,
    similarity,
)

_SECTIONS = ("基本信息", "教育背景", "项目经历", "专业技能")


def _suggestion(issue: str, priority: str = "high", advice: str = "改") -> Suggestion:
    return Suggestion(priority=priority, category="量化", issue=issue, advice=advice)


def _result(
    *,
    overall: int = 70,
    match: int = 60,
    issues: list[str] | None = None,
    sections: tuple[str, ...] = _SECTIONS,
    presents: tuple[bool, ...] | None = None,
) -> AnalysisResult:
    # AnalysisResult 要求至少 4 个栏目，所以 sections 不能少于 4 项 ——
    # 这也是为什么「单侧缺栏目」的用例要给两侧各 4 个、只是名字不同
    flags = presents or (True,) * len(sections)
    return AnalysisResult(
        overall_score=overall,
        summary="总评",
        completeness=[
            SectionCheck(name=name, present=flag, comment="已包含" if flag else "缺失")
            for name, flag in zip(sections, flags, strict=True)
        ],
        highlights=["亮点"],
        weaknesses=["短板"],
        match_score=match,
        match_reason="依据",
        suggestions=[_suggestion(issue) for issue in (issues or ["缺少量化数据"])],
    )


# ---------------------------------------------------------------- similarity


def test_similarity_ignores_punctuation_and_spacing() -> None:
    """模型两次输出几乎不可能标点完全一致，不归一化的话相似度会被无关字符拉低。"""
    assert similarity("缺少量化数据，建议补充", "缺少量化数据 建议补充") == 1.0


def test_similarity_is_case_insensitive() -> None:
    assert similarity("Missing Metrics", "missing metrics") == 1.0


def test_similarity_of_different_topics_stays_below_threshold() -> None:
    assert similarity("格式混乱", "缺少自我评价") < MATCH_THRESHOLD


def test_similarity_of_reworded_issue_stays_above_threshold() -> None:
    """「改了措辞的同一个问题」必须还能认出是同一条。"""
    assert similarity("缺少量化数据", "缺少量化数据，建议补充") >= MATCH_THRESHOLD


def test_similarity_handles_empty_string() -> None:
    assert similarity("", "任何内容") == 0.0


# ---------------------------------------------------------------- 建议配对


def test_compare_results_classifies_suggestions() -> None:
    base = _result(issues=["缺少量化数据", "格式混乱"])
    target = _result(issues=["缺少量化数据，建议补充", "缺少自我评价"])

    diff = compare_results(base, target)

    assert diff.stats.persisted_issues == 1
    assert diff.stats.resolved_issues == 1
    assert diff.stats.new_issues == 1


def test_compare_results_puts_persisted_first() -> None:
    base = _result(issues=["缺少量化数据", "格式混乱"])
    target = _result(issues=["缺少量化数据，建议补充", "缺少自我评价"])

    changes = [item.change for item in compare_results(base, target).suggestions]

    assert changes == ["persisted", "new", "resolved"]


def test_persisted_keeps_new_wording_and_priority_change() -> None:
    """配对成功后取**新**那条的文案：用户要看的是当前的措辞，
    同时把两侧优先级都带上，才能看出这条问题是不是变严重了。"""
    base = _result(issues=["缺少量化数据"])
    base.suggestions[0] = _suggestion("缺少量化数据", priority="low")
    target = _result(issues=["缺少量化数据，建议补充"])
    target.suggestions[0] = _suggestion("缺少量化数据，建议补充", priority="high")

    item = compare_results(base, target).suggestions[0]

    assert item.change == "persisted"
    assert item.issue == "缺少量化数据，建议补充"
    assert item.priority_before == "low"
    assert item.priority_after == "high"


# ---------------------------------------------------------------- 栏目完整度


def test_diff_sections_marks_unknown_when_one_side_missing() -> None:
    """「这次没检查教育背景」和「检查了、结论是缺失」是两件事。

    合成 False 会凭空造出一个「退步」。两侧栏目集合不同时取并集，
    单侧缺失的标 unknown。
    """
    base = _result(sections=("基本信息", "教育背景", "项目经历", "专业技能"))
    target = _result(sections=("基本信息", "教育背景", "项目经历", "自我评价"))

    changes = {item.name: item for item in diff_sections(base, target)}

    assert changes["专业技能"].change == "unknown"
    assert changes["专业技能"].after is None
    assert changes["自我评价"].change == "unknown"
    assert changes["自我评价"].before is None
    assert changes["基本信息"].change == "unchanged"


def test_diff_sections_detects_improved_and_worsened() -> None:
    base = _result(presents=(True, True, False, False))
    target = _result(presents=(True, False, True, False))

    changes = {item.name: item.change for item in diff_sections(base, target)}

    assert changes["基本信息"] == "unchanged"
    assert changes["教育背景"] == "worsened"
    assert changes["项目经历"] == "improved"
    assert changes["专业技能"] == "unchanged"


# ---------------------------------------------------------------- 分数与结论


def test_scores_delta_covers_both_metrics() -> None:
    diff = compare_results(_result(overall=60, match=50), _result(overall=75, match=48))

    scores = {item.key: item for item in diff.scores}

    assert scores["overall_score"].delta == 15
    assert scores["match_score"].delta == -2
    assert diff.stats.overall_delta == 15
    assert diff.stats.match_delta == -2


def test_verdict_separates_score_from_issue_counts() -> None:
    """分数和问题数经常背离（分数涨了但旧问题一个没改）。

    合成一句「进步了」会误导用户，所以两件事分开说。
    """
    base = _result(overall=60, issues=["缺少量化数据", "格式混乱"])
    target = _result(overall=70, issues=["缺少量化数据，建议补充"])

    verdict = compare_results(base, target).stats.verdict

    assert verdict.startswith("综合评分提升 10 分")
    assert "解决了 1 个问题" in verdict
    assert "仍有 1 个旧问题未处理" in verdict


def test_verdict_handles_flat_score() -> None:
    verdict = compare_results(_result(overall=70), _result(overall=70)).stats.verdict

    assert verdict.startswith("综合评分持平")
