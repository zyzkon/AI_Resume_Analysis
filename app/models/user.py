from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class User(Base):
    """用户表。

    只存认证必需的信息。邮箱唯一且建索引，因为登录时按邮箱查是最高频操作。
    密码永远只存 bcrypt 哈希，任何情况下都不落明文。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    resumes: Mapped[list["Resume"]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
    )
    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
