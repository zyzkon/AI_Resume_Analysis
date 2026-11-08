from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

if TYPE_CHECKING:
    # 这三个模型互相引用（user.resumes / resume.owner / analysis.user），
    # 运行时靠 relationship 里的字符串名解析，不需要真的导入，
    # 否则 user.py 导入 resume.py、resume.py 又导入 user.py，直接循环导入崩掉。
    # 但类型检查器看不懂"字符串形式的类名"，必须在这里给它一个真名，
    # 否则 mypy 会报 Name "Resume" is not defined
    from app.models.analysis import Analysis
    from app.models.resume import Resume


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
