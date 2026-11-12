"""开发期建表脚本。

表结构的唯一真相是 alembic/versions/ 下的迁移脚本，日常改表请走 Alembic
（见 README「数据库迁移」）。这个脚本只保留两个用途：

1. 空库想跳过迁移链、直接按模型建表
2. 手工验证模型定义写得对不对

注意 create_all 只创建"不存在"的表，不会修改已有表的结构 —— 模型里加了字段，
它既不会补列也不会报错，应用直到第一次查那一列才炸。所以它绝不能用于已有数据的环境。

用法：python -m app.db.init_db
"""

from app.db.session import Base, engine
from app.models import Analysis, AnalysisStatus, Resume, User  # noqa: F401  导入即注册表


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
    print("建表完成：", ", ".join(sorted(Base.metadata.tables)))
