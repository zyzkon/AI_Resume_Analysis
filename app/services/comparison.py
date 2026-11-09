"""两次分析的差异计算。

设计原则：这里只有纯函数。
不连数据库、不 import FastAPI、不碰 Celery —— 输入是两个已经校验过的
AnalysisResult，输出是 schemas/comparison.py 里的模型。
好处是将来写单元测试时可以直接构造两个 AnalysisResult 调它，
不用起 MySQL、不用起 Redis。

为什么不直接把结果塞给前端让它自己 diff：
1. 建议的匹配需要模糊判断（"缺少量化数据"和"项目描述没有数字"是同一个问题），
   这种判断放在前端就要用 JS 再实现一遍相似度算法，两边行为必然不一致
2. verdict、排序、计数的规则属于业务，散到前端就再也改不动了
"""

import logging
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.schemas.analysis import AnalysisResult, Suggestion
from app.schemas.comparison import (
    CompareStats,
    ScoreDelta,
    SectionChange,
    SectionDelta,
    SuggestionChange,
    SuggestionDelta,
)

logger = logging.getLogger(__name__)

# 相似度阈值。定在 0.55 的理由：
# 同一句话改了措辞（"建议补充量化数据" vs "缺少量化数据，建议补充"）通常在 0.6~0.8；
# 只是话题相同的两个不同问题（"项目缺少量化" vs "技能描述缺少量化"）通常在 0.4 以下。
# 0.55 落在两者之间。调高会把"已改进但仍相关"误判成两条，调低会把两个问题合成一条
MATCH_THRESHOLD = 0.55

# 比较前先去掉标点和空白：模型两次输出几乎不可能标点完全一致，
# 不归一化的话相似度会被这些无关字符拉低
_PUNCT = str.maketrans("", "", "，。、；：？！“”‘’（）《》【】…—～·「」『』,.;:?!'\"()[]{}<>-_/\\| \t\r\n")

# 优先级排序权重：high 的问题应该最先被看到
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def similarity(a: str, b: str) -> float:
    """两段文本的相似度，0~1。

    用标准库的 SequenceMatcher（最长公共子序列的变体），字符级比较。
    中文没有空格分词，字符级反而比分词更稳 —— 不需要额外的分词库，
    也不会因为分词器把"量化数据"拆成两个词而丢掉整体相似性。
    """
    na, nb = a.translate(_PUNCT).lower(), b.translate(_PUNCT).lower()
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


@dataclass(slots=True)
class _Pair:
    """一条建议在两次分析里的对应关系。两侧都为 None 的情况不会出现。"""

    before: Suggestion | None = None
    after: Suggestion | None = None
    score: float = 0.0


def match_suggestions(
    before: list[Suggestion],
    after: list[Suggestion],
) -> list[_Pair]:
    """把两次分析的建议两两配对。

    用的是贪心匹配而不是最优匹配（匈牙利算法）：每条建议最多 8 条，
    最优匹配要算 8! 量级的组合，而贪心的结果在这种规模下几乎没差别。

    每条旧建议找出与之最相似、且尚未被占用的新建议；
    相似度达标就算"仍存在"，否则算"已解决"。剩下的新建议算"新增"。

    一个已知的局限：如果两次只给出 1 条建议但话题完全不同，
    会因为相似度低于阈值而变成「1 条已解决 + 1 条新增」，
    看起来像"问题变了"，实际是"模型这次关注点变了"。
    这是文本对比的固有局限，不值得为此引入向量检索。
    """
    pairs: list[_Pair] = []
    claimed: set[int] = set()

    for old in before:
        best_index, best_score = -1, 0.0
        for index, new in enumerate(after):
            if index in claimed:
                continue
            score = similarity(old.issue, new.issue)
            if score > best_score:
                best_index, best_score = index, score
        if best_index >= 0 and best_score >= MATCH_THRESHOLD:
            claimed.add(best_index)
            pairs.append(_Pair(before=old, after=after[best_index], score=best_score))
        else:
            pairs.append(_Pair(before=old))

    pairs.extend(_Pair(after=new) for index, new in enumerate(after) if index not in claimed)
    return pairs


def _to_delta(pair: _Pair) -> SuggestionDelta:
    """把配对结果翻译成出参。

    排序靠前的应该是"仍存在" —— 那意味着上次的建议没落地，
    是用户下一步最该动手的地方；"新增"次之；"已解决"放最后，
    那是用来给用户正反馈的，不该占视觉首位。
    """
    if pair.before is not None and pair.after is not None:
        change: SuggestionChange = "persisted"
        issue, advice, example = pair.after.issue, pair.after.advice, pair.after.example
        category = pair.after.category
        return SuggestionDelta(
            change=change,
            category=category,
            issue=issue,
            advice=advice,
            example=example,
            priority_before=pair.before.priority,
            priority_after=pair.after.priority,
            similarity=round(pair.score, 3),
        )

    if pair.after is not None:
        change = "new"
        return SuggestionDelta(
            change=change,
            category=pair.after.category,
            issue=pair.after.issue,
            advice=pair.after.advice,
            example=pair.after.example,
            priority_after=pair.after.priority,
        )

    # 走到这里只可能是"已解决"：before 有、after 没有。
    # match_suggestions 保证不会产生两侧都为 None 的配对，这里断言一下让类型收窄
    assert pair.before is not None
    change = "resolved"
    return SuggestionDelta(
        change=change,
        category=pair.before.category,
        issue=pair.before.issue,
        advice=pair.before.advice,
        example=pair.before.example,
        priority_before=pair.before.priority,
    )


def _delta_order(item: SuggestionDelta) -> tuple[int, int]:
    """排序键：先按变化类型，再按优先级。

    persisted 权重最高（0），其次是 new（1），resolved 最后（2）。
    同组内按优先级，high 在前。
    """
    change_rank = {"persisted": 0, "new": 1, "resolved": 2}[item.change]
    priority = item.priority_after or item.priority_before or "medium"
    return change_rank, _PRIORITY_ORDER.get(priority, 1)


def diff_sections(base: AnalysisResult, target: AnalysisResult) -> list[SectionDelta]:
    """逐栏目对比完整度。

    两个列表的栏目集合可能不同（模型每次挑的栏目不固定），
    所以取并集，单侧缺失的标记成 unknown 而不是当作 False ——
    "这次没检查教育背景"和"这次检查了、结论是缺失"是完全不同的两件事。
    """
    before_map = {item.name: item.present for item in base.completeness}
    after_map = {item.name: item.present for item in target.completeness}

    names = list(dict.fromkeys(
        [item.name for item in base.completeness] + [item.name for item in target.completeness]
    ))

    result: list[SectionDelta] = []
    for name in names:
        before = before_map.get(name)
        after = after_map.get(name)
        change: SectionChange
        if before is None or after is None:
            change = "unknown"
        elif before == after:
            change = "unchanged"
        elif after:
            change = "improved"
        else:
            change = "worsened"
        result.append(SectionDelta(name=name, before=before, after=after, change=change))
    return result


def _build_verdict(overall_delta: int, new_issues: int, resolved: int, persisted: int) -> str:
    """一句话结论。

    刻意把"分数"和"问题数"分开说：这两个指标经常背离
    （分数涨了但旧问题一个没改），合成一句"进步了"会误导用户。
    """
    if overall_delta > 0:
        head = f"综合评分提升 {overall_delta} 分"
    elif overall_delta < 0:
        head = f"综合评分下降 {abs(overall_delta)} 分"
    else:
        head = "综合评分持平"

    parts = [head]
    if resolved:
        parts.append(f"解决了 {resolved} 个问题")
    if new_issues:
        parts.append(f"新增 {new_issues} 个问题")
    if persisted:
        parts.append(f"仍有 {persisted} 个旧问题未处理")
    return "，".join(parts) + "。"


@dataclass(slots=True)
class Diff:
    """对比结果的四个部分，路由层再补上双方摘要拼成 CompareOut。"""

    stats: CompareStats
    scores: list[ScoreDelta]
    sections: list[SectionDelta]
    suggestions: list[SuggestionDelta]


def compare_results(base: AnalysisResult, target: AnalysisResult) -> Diff:
    """把两次分析结果算成差异。base 是基准（较早），target 是对比（较新）。"""
    scores = [
        ScoreDelta(
            key="overall_score",
            label="综合评分",
            before=base.overall_score,
            after=target.overall_score,
            delta=target.overall_score - base.overall_score,
        ),
        ScoreDelta(
            key="match_score",
            label="岗位匹配度",
            before=base.match_score,
            after=target.match_score,
            delta=target.match_score - base.match_score,
        ),
    ]

    deltas = sorted((_to_delta(pair) for pair in match_suggestions(base.suggestions, target.suggestions)), key=_delta_order)
    counts = {"persisted": 0, "new": 0, "resolved": 0}
    for item in deltas:
        counts[item.change] += 1

    stats = CompareStats(
        overall_delta=target.overall_score - base.overall_score,
        match_delta=target.match_score - base.match_score,
        new_issues=counts["new"],
        resolved_issues=counts["resolved"],
        persisted_issues=counts["persisted"],
        verdict=_build_verdict(
            overall_delta=target.overall_score - base.overall_score,
            new_issues=counts["new"],
            resolved=counts["resolved"],
            persisted=counts["persisted"],
        ),
    )

    return Diff(
        scores=scores,
        sections=diff_sections(base, target),
        suggestions=deltas,
        stats=stats,
    )
