"""建表脚本。

create_all 只会创建"不存在"的表，不会修改已有表的结构。
所以它只适合开发初期表结构还在变的时候用；一旦上线，改字段必须交给 Alembic。

用法：python -m app.db.init_db
"""

from app.db.session import Base, engine
from app.models import Analysis, AnalysisStatus, Resume, User  # noqa: F401  导入即注册表

def init_db() -> None:
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
    print("建表完成：", ", ".join(sorted(Base.metadata.tables)))
