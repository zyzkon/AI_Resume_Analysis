from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

if TYPE_CHECKING:
    # 仅给类型检查器用，运行时不导入（避免模型间的循环导入）。
    # 细节见 app/models/user.py 里的同一段注释
    from app.models.resume import Resume
    from app.models.user import User


class AnalysisStatus(StrEnum):
    """分析任务的状态机。

    继承 StrEnum 而不是普通 Enum，是为了让它能直接当字符串存进 String 列，
    不需要额外的类型转换器；同时代码里写 AnalysisStatus.PENDING 比写裸字符串安全，
    IDE 能补全、拼错会报错。
    """

    PENDING = "pending"     # 已建记录，等 worker 接手
    RUNNING = "running"     # worker 已开始调用模型
    DONE = "done"           # 分析成功，result 有值
    FAILED = "failed"       # 分析失败，error_message 有值


class Analysis(Base):
    """分析结果表。

    status 是这张表的灵魂：接口创建记录后立刻返回，前端靠轮询这个字段判断进度。
    没有 running 这个中间态，就分不清"排队中"和"正在跑"，超时也没法判断该不该重试。

    result 用 JSON 列而不是拆成十几个字段，因为 Prompt 的输出结构还会反复迭代，
    拆成列意味着每改一次 Prompt 就要改一次表结构；JSON 列则只改 Pydantic 模型。
    """

    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(primary_key=True)

    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"),
        index=True,
    )
    # 刻意冗余一列：通过 resume_id 能关联出 user_id，但"查我的所有分析记录"
    # 是最高频查询，冗余这一列可以省掉一次 join；简历被删后记录也还能独立追溯。
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )

    # 用户填写的目标岗位 JD，可以为空（不做岗位匹配时只输出通用分析）
    jd_text: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(
        String(20), default=AnalysisStatus.PENDING, server_default=AnalysisStatus.PENDING
    )

    # 完整的结构化分析结果，形状由 app/schemas/analysis.py 里的 Pydantic 模型约束
    result: Mapped[dict | None] = mapped_column(JSON)

    # 从 result 里单独抽出来的一列，只为了列表页能直接按匹配度排序。
    # SQLite 的 JSON 函数能力有限，跨数据库也不通用，冗余一个 int 更省事。
    match_score: Mapped[int | None] = mapped_column(Integer)

    error_message: Mapped[str | None] = mapped_column(Text)

    # 记录是哪次分析用了哪个模型、哪版 Prompt —— 没有这两列，历史结果之间没法比较
    model_name: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(
        String(20), default="v1", server_default="v1"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # worker 真正开始跑的时间，用来算排队耗时和实际耗时
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    resume: Mapped["Resume"] = relationship(back_populates="analyses")
    user: Mapped["User"] = relationship(back_populates="analyses")
