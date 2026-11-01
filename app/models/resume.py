from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Resume(Base):
    """简历表。

    同时承载两样东西：磁盘上的文件和解析后的纯文本。
    raw_text 单独存一份，是为了分析时直接查库取用，不必每次重新解析 PDF。
    stored_filename 用 UUID 生成，和用户上传的原始文件名解耦，
    既避免同名文件互相覆盖，也避免文件名里带 ../ 造成路径穿越。
    """

    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )

    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255), unique=True)

    file_size: Mapped[int] = mapped_column()
    char_count: Mapped[int] = mapped_column(default=0)

    raw_text: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    owner: Mapped["User"] = relationship(back_populates="resumes")
    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
    )
