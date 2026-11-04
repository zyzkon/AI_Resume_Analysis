from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    # 从池里取连接前先探活。MySQL 有 wait_timeout（默认 8 小时），
    # 中间的网络设备往往更短，闲置连接会被单方面断开，
    # 不探活的话下一次请求会拿到死连接直接报 OperationalError。
    pool_pre_ping=True,
    # 连接用满 1 小时就主动丢弃重建。pre_ping 只能救"已经被断"的连接，
    # recycle 是从根上不让连接活到被断的时候，两者配合才算稳。
    pool_recycle=3600,
    # 常驻连接数。同步路由跑在线程池里，默认线程池 40，
    # 这里 10+20=30 上限够用，又不会把 MySQL 的 max_connections 打满。
    pool_size=10,
    max_overflow=20,
    # 池子耗尽时最多等多久，超时抛 TimeoutError 而不是无限挂住请求
    pool_timeout=30,
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
