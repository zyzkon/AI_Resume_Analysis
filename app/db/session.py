from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

engine = create_engine(
    settings.DATABASE_URL,
    # SQLite 默认禁止跨线程复用连接，而 FastAPI 的同步路由跑在线程池里
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    # 从池里取连接前先探活，避免拿到被服务端单方面断开的老连接
    pool_pre_ping=True,
    echo=settings.DB_ECHO,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,      # 不让查询前的隐式 flush 打乱写入顺序
    autocommit=False,     # SQLAlchemy 2.x 已移除该参数的实际语义，显式写出来是给读代码的人看
    expire_on_commit=False,  # commit 后对象属性不过期，避免序列化时再次查库或 DetachedInstanceError
)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类，metadata 挂在这里。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：每个请求一个 Session，请求结束必定关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
