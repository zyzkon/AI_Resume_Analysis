from collections.abc import Generator

from anyio import to_thread
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
    # 常驻连接数与溢出上限都走配置，因为这两个数不只是"池子大小"，
    # 它们同时决定了这个进程能并发处理多少个请求（见下面的 MAX_DB_CONNECTIONS）
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # 池子耗尽时最多等多久，超时抛 TimeoutError 而不是无限挂住请求。
    # 线程池与连接池对齐之后这里不该被触发，它的作用是"不变量一旦破了就快报错"
    pool_timeout=settings.DB_POOL_TIMEOUT,
    echo=settings.DB_ECHO,
)

# 单个进程最多持有的连接数。它必须 **大于** 并发闸门（在途请求数），
# 因为一条请求在两个线程池阶段之间仍然held着连接，占连接的请求可以比
# 正在跑的线程多。三层关系是：闸门 ≤ 线程池 < 连接池
MAX_DB_CONNECTIONS = settings.db_max_connections


def align_thread_pool(limit: int) -> None:
    """把 anyio 线程池容量对齐到并发闸门，让"在途请求 ≤ 线程"这个等式成立。

    为什么是闸门而不是连接池 —— 2026-11 压测实测出来的：
    最早池上限 30（10+20），anyio 默认 40 线程，200 并发下 2% 请求卡满 30 秒。
    把池加到 40、线程也设成 40 之后**反而更差**（12% 失败）：40 个线程全部
    拿到连接，而占着连接的请求数继续上涨，池再次被抽干，这次连"多出来的
    10 个线程在排队"这个缓冲都没了。

    原因在 app/core/concurrency.py 里说透了：FastAPI 的同步依赖、同步路由、
    依赖清理是分三次进线程池的，只有第一次查库时拿连接、最后一次才归还，
    中间那段时间线程已经归还但连接没有。所以要限的是**请求数**，不是线程数。

    必须在事件循环里调用（lifespan 的 startup 满足条件）：anyio 的默认
    limiter 存在 RunVar 里，与当前上下文绑定，在线程里设置是无效的。
    """
    to_thread.current_default_thread_limiter().total_tokens = limit


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
