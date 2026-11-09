"""历史对比的数据结构：两次分析的差异，以及一份简历的分数走势。

这里和 analysis.py 的分工：
analysis.py 描述"模型输出什么"，字段由 Prompt 决定；
这里描述"两次输出之间发生了什么变化"，字段由产品需求决定 ——
用户真正想知道的是「我按上次的建议改了，到底有没有变好」。

全部字段都是从已有的 result JSON 里算出来的，不新增数据库列。
"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.analysis import Category, Priority

# 只对比这两个总分维度。
# 不把 completeness 的栏目也算成"分数"，因为它是布尔值，
# 混进分数列表会让前端的图表要么画错要么得写特殊判断
ScoreKey = Literal["overall_score", "match_score"]

# 栏目变化：improved 缺失->出现，worsened 出现->缺失，
# unknown 表示某一次分析压根没检查这个栏目（模型每次挑的栏目可能不同）
SectionChange = Literal["improved", "worsened", "unchanged", "unknown"]

# 建议变化：new 新出现，resolved 已解决，persisted 两次都存在
SuggestionChange = Literal["new", "resolved", "persisted"]


class ScoreDelta(BaseModel):
    """一个分数维度在两次分析之间的变化。"""

    key: ScoreKey
    label: str = Field(description="中文名，前端直接拿去当图表标签")
    before: int
    after: int
    delta: int = Field(description="after - before，正数代表变好")


class SectionDelta(BaseModel):
    """某一个栏目（教育背景、项目经历……）的完整度变化。"""

    name: str
    before: bool | None = Field(default=None, description="基准分析里是否出现；null 表示那次没检查")
    after: bool | None = Field(default=None, description="对比分析里是否出现；null 表示那次没检查")
    change: SectionChange


class SuggestionDelta(BaseModel):
    """一条建议的归属变化。

    issue / advice 统一用"对比分析"（也就是较新那份）的文本：
    用户看的是「现在该改成什么样」，而不是「上次是怎么说的」。
    已解决（resolved）的那几条只有 before 一侧有文本，就退回用旧的。
    """

    change: SuggestionChange
    category: Category
    issue: str
    advice: str
    example: str | None = None
    priority_before: Priority | None = None
    priority_after: Priority | None = None
    similarity: float = Field(
        default=0.0,
        ge=0,
        le=1,
        description="persisted 时的文本相似度；new/resolved 恒为 0",
    )


class CompareStats(BaseModel):
    """给前端做徽标用的汇总数字，省得它自己遍历一遍列表。"""

    overall_delta: int = Field(description="综合评分变化，正数代表进步")
    match_delta: int = Field(description="岗位匹配度变化")
    new_issues: int
    resolved_issues: int
    persisted_issues: int
    verdict: str = Field(description="一句话结论，可直接展示给用户")


class AnalysisBrief(BaseModel):
    """参与对比的一次分析的摘要。不带 result 全文 —— 对比接口已经用不上它。"""

    id: int
    created_at: datetime
    model_name: str | None = None
    prompt_version: str
    jd_text: str | None = None
    overall_score: int
    match_score: int
    suggestion_count: int

    @field_validator("created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        """MySQL 的 DATETIME 不存时区，读出来是 naive 的，补上 UTC 标记。"""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class CompareOut(BaseModel):
    """两次分析的完整对比结果。"""

    resume_id: int
    base: AnalysisBrief = Field(description="基准（较早的一次）")
    target: AnalysisBrief = Field(description="对比（较新的一次）")
    scores: list[ScoreDelta]
    sections: list[SectionDelta]
    suggestions: list[SuggestionDelta]
    stats: CompareStats


class TrendPoint(BaseModel):
    """走势图上的一个点。字段刻意保持扁平，前端不用再嵌套取值。"""

    analysis_id: int
    created_at: datetime
    overall_score: int
    match_score: int
    suggestion_count: int
    model_name: str | None = None
    prompt_version: str

    @field_validator("created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class TrendOut(BaseModel):
    """一份简历全部分析的走势。

    points 按时间正序 —— 折线图从左到右应该就是时间推进的方向。
    查库时按倒序取最近 N 条再翻转，这样 limit 限制的是"最近"，不是"最早"。
    """

    resume_id: int
    points: list[TrendPoint]
    best_analysis_id: int | None = Field(default=None, description="综合评分最高的一次")
    latest_analysis_id: int | None = Field(default=None, description="最近完成的一次")
