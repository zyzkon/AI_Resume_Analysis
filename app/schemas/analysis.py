"""AI 分析结果的结构定义。

这是整个项目里唯一「由大模型填写」的数据结构，所以它和普通 schema 的侧重点不同：
普通 schema 用来描述我们自己想返回什么，这里要先解决「模型可能乱填」的问题。

三条防线，从宽到严：
1. 数值越界（打分给 105）     -> 钳到合法区间，不值得为它重试一次调用
2. 枚举写错（优先级写成 urgent）-> 归一到最近似的合法值
3. 列表过长（给了 20 条建议）  -> 截断，防止响应体膨胀
4. 根本不是 JSON / 缺字段      -> 只能重试，交给 llm.py 的循环处理
"""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 状态枚举从 models 里复用，不在这里重复定义一份 Literal。
# 两份定义在项目里并存的话，某天加了个新状态，必然有一处忘记同步。
from app.models.analysis import AnalysisStatus

# 一份合格的技术岗简历应该覆盖的栏目。
# 固定成常量而不是让模型自由发挥，是为了让不同简历的检查结果能横向对比。
REQUIRED_SECTIONS: tuple[str, ...] = (
    "基本信息",
    "教育背景",
    "工作/实习经历",
    "项目经历",
    "专业技能",
    "自我评价",
)

Priority = Literal["high", "medium", "low"]
Category = Literal["内容", "表达", "结构", "量化", "格式"]


class SectionCheck(BaseModel):
    """简历里某一个栏目的检查结果。"""

    name: str = Field(description="栏目名，如「教育背景」")
    present: bool = Field(description="这一栏目在简历里是否出现")
    comment: str = Field(description="一句话评价；缺失时说明该补什么")


class Suggestion(BaseModel):
    """一条可执行的修改建议。

    刻意拆成 issue + advice + example 三段，而不是一段话：
    issue 让用户认得出「说的是我简历里哪句话」，
    advice 告诉他怎么改，
    example 给出可以直接抄的改写示范。
    三合一的大段文字，用户读完还是不知道从哪下手。
    """

    priority: Priority = Field(description="改动收益，high 的应该先做")
    category: Category
    issue: str = Field(description="具体问题，尽量引述简历原文")
    advice: str = Field(description="怎么改")
    example: str | None = Field(default=None, description="改写示例，没有就留空")

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_priority(cls, value: object) -> object:
        """模型写成 urgent / 高 / HIGH 都收下，映射到三档。

        这个字段只影响建议的排序，认错了顶多顺序差一点；
        但如果因为这个词非法而让整条校验失败，代价是重试一次模型调用。
        """
        if not isinstance(value, str):
            return value
        mapping = {
            "high": "high", "h": "high", "urgent": "high", "critical": "high",
            "important": "high", "高": "high", "紧急": "high", "关键": "high",
            "low": "low", "l": "low", "minor": "low", "optional": "low",
            "低": "low", "次要": "low",
        }
        return mapping.get(value.strip().lower(), "medium")

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text in ("内容", "表达", "结构", "量化", "格式"):
            return text
        mapping = {
            "完整性": "内容", "信息": "内容", "经历": "内容",
            "语言": "表达", "措辞": "表达", "表述": "表达", "表达方式": "表达",
            "布局": "结构", "排版": "结构", "组织": "结构", "顺序": "结构",
            "数据": "量化", "数字": "量化", "成果": "量化",
            "样式": "格式", "外观": "格式",
        }
        return mapping.get(text, "内容")


class AnalysisResult(BaseModel):
    """一次完整分析的产出，直接存进 analyses.result 这个 JSON 列。"""

    overall_score: int = Field(ge=0, le=100, description="综合评分")
    summary: str = Field(description="80 字以内的总评")
    completeness: list[SectionCheck] = Field(description="逐栏目检查")
    highlights: list[str] = Field(description="做得好的地方，2-5 条")
    weaknesses: list[str] = Field(description="短板，2-5 条")
    match_score: int = Field(ge=0, le=100, description="与目标岗位的匹配度")
    match_reason: str = Field(description="匹配度结论的依据")
    suggestions: list[Suggestion] = Field(description="可执行的修改建议，按优先级排序")

    @field_validator("overall_score", "match_score", mode="before")
    @classmethod
    def _clamp_score(cls, value: object) -> object:
        """模型偶尔会写 105 或 -3，钳住比报错重试划算。

        重试要再花几秒和一份 token，而钳完之后的信息损失可以忽略。
        非数字的情况放过去，让 Pydantic 按类型错误正常报出来。
        """
        if isinstance(value, bool):  # bool 是 int 的子类，单独排掉
            return value
        if isinstance(value, (int, float)):
            return max(0, min(100, int(round(value))))
        return value

    @field_validator("summary", "match_reason")
    @classmethod
    def _trim_text(cls, value: str) -> str:
        value = value.strip()
        return value if len(value) <= 300 else value[:300] + "…"

    @field_validator("highlights", "weaknesses")
    @classmethod
    def _limit_items(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("至少需要一条")
        return cleaned[:5]

    @field_validator("suggestions")
    @classmethod
    def _limit_suggestions(cls, value: list[Suggestion]) -> list[Suggestion]:
        if not value:
            raise ValueError("至少要给出一条建议")
        return value[:8]

    @field_validator("completeness")
    @classmethod
    def _check_completeness(cls, value: list[SectionCheck]) -> list[SectionCheck]:
        """防止模型只检查一个栏目就交差。

        不强制必须覆盖 REQUIRED_SECTIONS 全部六项（不同岗位侧重不同，
        强行要求会逼模型编造），但少于 4 项说明它没认真做。
        """
        if len(value) < 4:
            raise ValueError(f"至少需要检查 4 个栏目，只收到 {len(value)} 个")
        return value[:8]


# ---------------------------------------------------------------- 接口出入参

# 文档示例用的完整结果。写成模块级常量而不是内联：
# AnalysisOut 和（将来的）其他地方都要用它，内联的话改一处漏一处。
# 内容按真实 mock 输出的形状写 —— 示例和实际响应差太远，比没有示例更误导人。
_RESULT_EXAMPLE: dict[str, Any] = {
    "overall_score": 62,
    "summary": "后端技术栈与目标岗位基本匹配；项目描述偏重功能罗列，缺少量化结果。",
    "completeness": [
        {"name": "基本信息", "present": True, "comment": "姓名、手机、邮箱齐全"},
        {"name": "教育背景", "present": True, "comment": "学校与专业完整，缺 GPA 或排名"},
        {
            "name": "工作/实习经历",
            "present": True,
            "comment": "有两段实习，但只写了做了什么，没写结果",
        },
        {"name": "项目经历", "present": True, "comment": "项目描述缺少技术选型的理由"},
        {"name": "专业技能", "present": True, "comment": "技能罗列完整，未标注熟练度"},
        {"name": "自我评价", "present": False, "comment": "建议补一段，两行以内即可"},
    ],
    "highlights": ["技术栈与岗位高度重合", "有真实上线的后端项目"],
    "weaknesses": ["项目成果缺少量化指标", "工作经历描述过于笼统"],
    "match_score": 58,
    "match_reason": "岗位要求的 FastAPI 与 SQLAlchemy 都有实际使用经历，"
    "但缺少 LLM 应用经验，学历符合要求。",
    "suggestions": [
        {
            "priority": "high",
            "category": "量化",
            "issue": "项目经历写「优化了接口性能」，没有给出优化前后的对比",
            "advice": "补上具体指标：QPS、响应时间、错误率，至少给两个",
            "example": "订单查询接口 P99 从 480ms 降到 120ms，QPS 由 300 提升至 1200",
        },
        {
            "priority": "medium",
            "category": "内容",
            "issue": "两段实习都用「负责 XX 模块开发」开头，看不出你的独立贡献",
            "advice": "改成「问题 - 做法 - 结果」的结构，交代清楚为什么这么做",
            "example": "订单超时频发：引入延迟队列做超时关单，超时投诉下降 90%",
        },
        {
            "priority": "low",
            "category": "结构",
            "issue": "缺少自我评价，简历结尾显得突兀",
            "advice": "补两行，说明技术方向与求职意向，不要写「吃苦耐劳」这类空话",
            "example": None,
        },
    ],
}


class AnalysisCreate(BaseModel):
    """提交分析时的请求体。

    只有一个字段，而且整体可以不传 —— 前端直接 POST 空 body 也能按通用标准跑一遍。
    """

    job_description: str | None = Field(
        default=None,
        max_length=2000,
        description="目标岗位 JD。不传则按通用技术岗标准评估匹配度",
    )

    @field_validator("job_description")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        """空字符串和纯空格都当作"没填"。

        前端表单未填写时通常提交 ""，原样存进 jd_text 的话，
        Prompt 里会多出一段空的【目标岗位】，模型的判断容易被这段空白带偏。
        """
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_description": (
                    "招聘 Python 后端工程师：熟悉 FastAPI / SQLAlchemy / MySQL，"
                    "有 LLM 应用经验优先，本科及以上学历。"
                )
            }
        }
    )


class AnalysisOut(BaseModel):
    """分析记录的完整出参。

    result 从 JSON 列读出来是个 dict，这里直接声明成 AnalysisResult，
    Pydantic 会把它再校验一遍 —— 相当于对"几个月前存进去的数据"
    做一次结构回归测试。模型改过字段之后，老数据能不能读出来一目了然。
    """

    id: int
    resume_id: int
    status: AnalysisStatus
    jd_text: str | None = None
    match_score: int | None = Field(default=None, description="从 result 抽出的冗余列")
    result: AnalysisResult | None = Field(default=None, description="完整分析结果，未完成时为 null")
    model_name: str | None = None
    prompt_version: str
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 12,
                "resume_id": 2,
                "status": "done",
                "jd_text": "招聘 Python 后端工程师：熟悉 FastAPI / SQLAlchemy / MySQL…",
                "match_score": 58,
                "result": _RESULT_EXAMPLE,
                "model_name": "deepseek-chat",
                "prompt_version": "v1.0.0",
                "error_message": None,
                "created_at": "2026-11-08T09:14:02Z",
                "started_at": "2026-11-08T09:14:03Z",
                "finished_at": "2026-11-08T09:14:19Z",
            }
        },
    )

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def _ensure_utc(cls, value: datetime | None) -> datetime | None:
        """MySQL 的 DATETIME 不存时区，读出来是 naive 的，补上 UTC 标记。

        和 ResumeOut 里那份是同一套逻辑，但三个字段里有两个可为 None，
        所以要先判空，不能照抄。
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class AnalysisListItem(BaseModel):
    """历史列表里的一条。

    刻意不带 result 全文：一次分析的结果 JSON 有一两千字，
    20 条就是几十 KB，而列表页只想回答"哪次分析、几分、什么状态"。
    overall_score 和 summary 是从 result 里挑出来的两个摘要字段。
    """

    id: int
    resume_id: int
    status: AnalysisStatus
    match_score: int | None = None
    overall_score: int | None = None
    summary: str | None = None
    created_at: datetime

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": 12,
                "resume_id": 2,
                "status": "done",
                "match_score": 58,
                "overall_score": 62,
                "summary": "后端技术栈与目标岗位基本匹配；项目描述偏重功能罗列，缺少量化结果。",
                "created_at": "2026-11-08T09:14:02Z",
            }
        }
    )

    @field_validator("created_at")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class AnalysisListOut(BaseModel):
    """分页外壳，形状和 ResumeListOut 保持一致，前端不用记两套。"""

    total: int
    page: int
    size: int
    items: list[AnalysisListItem]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total": 3,
                "page": 1,
                "size": 20,
                "items": [
                    {
                        "id": 12,
                        "resume_id": 2,
                        "status": "done",
                        "match_score": 58,
                        "overall_score": 62,
                        "summary": "后端技术栈与目标岗位基本匹配；项目描述偏重功能罗列，缺少量化结果。",
                        "created_at": "2026-11-08T09:14:02Z",
                    },
                    {
                        "id": 11,
                        "resume_id": 2,
                        "status": "failed",
                        "match_score": None,
                        "overall_score": None,
                        "summary": None,
                        "created_at": "2026-11-08T08:02:11Z",
                    },
                ],
            }
        }
    )
