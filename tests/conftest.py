"""测试公共装置：把外部依赖换成进程内的替身。

三轮替换，各解决一件事：

1. **SQLite 内存库替 MySQL** —— 不用起 docker，每个用例从空表开始
2. **依赖覆盖替 get_db** —— API 用例拿到的 Session 指向内存库
3. **LLM_PROVIDER=mock** —— 不联网、不花钱，结果由正文决定，可重复

fixture 是串起来的：engine -> db -> client。client 通过 app.dependency_overrides
把 db 注入进去，所以同一个用例里 client 和 db 看到的是同一个会话。

环境变量必须写在 import app.* 之前：settings 是模块级单例，
`settings = get_settings()` 在 config.py 被 import 的那一刻就执行完了，
之后再改环境变量完全没有效果。
"""

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

os.environ["LLM_PROVIDER"] = "mock"
# 上传目录指到系统临时目录，测试产生的文件不会堆进项目的 uploads/
os.environ["UPLOAD_DIR"] = str(Path(tempfile.gettempdir()) / "resume-analyzer-tests")
# .env 里通常有 SECRET_KEY，但 CI 环境没有；setdefault 不会覆盖已有的值
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")

# 前端来源写死，别让用例受本机 .env 影响 —— 否则「这台机器全绿、换台机器全红」
CORS_ALLOWED = "http://localhost:5173"
CORS_ALLOWED_ALT = "http://127.0.0.1:5173"
CORS_BLOCKED = "https://evil.example"
os.environ["CORS_ORIGINS"] = f"{CORS_ALLOWED},{CORS_ALLOWED_ALT}"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import create_access_token, hash_password
from app.db.session import Base, get_db
from app.main import app
from app.models.resume import Resume
from app.models.user import User

PASSWORD = "test-password-1234"
# bcrypt 一次约 100ms（默认 12 轮），每个用例都重算太亏。
# 哈希值只跟密码有关、和用户 id 无关，算一次反复用
_HASHED_PASSWORD = hash_password(PASSWORD)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """整个测试进程共用一个内存库。

    两个参数都不是可有可无的：

    - **StaticPool**：`sqlite://` 的内存库是「每个连接一个独立数据库」。
      TestClient 在另一个线程里跑请求，默认连接池会给它新开一个连接，
      那个连接看到的是空库，报 `no such table: users`。
      StaticPool 让所有线程共用同一个连接，表才两边可见。
    - **check_same_thread=False**：允许这个连接跨线程使用，配合上一条。

    `PRAGMA foreign_keys=ON` 必须挂在 connect 事件上：SQLite **默认不启用外键**，
    不开的话 `ON DELETE CASCADE` 完全不生效，级联删除的用例会假通过。
    """
    test_engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(test_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    yield test_engine
    test_engine.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """每个用例一套干净的表。

    粒度选「建表 / 删表」而不是「事务回滚」：三张表的 DDL 在内存库上只要几毫秒，
    而回滚方案要在路由里那些 commit() 之上套一层 SAVEPOINT，
    对读测试的人是额外理解成本，收益几乎为零。
    """
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    """TestClient，所有请求共用上面那个 db 会话。

    覆盖的是 FastAPI 的依赖 `get_db`，不是 `app.db.session.SessionLocal` ——
    后者被 `from ... import SessionLocal` 复制进了 `workers/tasks.py` 的命名空间，
    在这里 patch 对那边无效。worker 路径的用例应该直接调
    `execute_analysis(db, id)`，从根上绕开 SessionLocal。

    用 pop 而不是 clear()：clear 会把其他用例挂的覆盖一起清掉。
    """
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def password() -> str:
    """`user` 这个账号的明文密码。

    单独做成 fixture 而不是让测试文件自己写死字符串：改一处就够，
    也不会出现「conftest 改了密码、登录用例还拿旧密码跑」这种假失败。
    """
    return PASSWORD


@pytest.fixture
def user(db: Session) -> User:
    """一个已注册用户。测试登录接口时可以直接拿 PASSWORD 当明文。"""
    created = User(email="tester@example.com", hashed_password=_HASHED_PASSWORD)
    db.add(created)
    db.commit()
    db.refresh(created)
    return created


@pytest.fixture
def auth_headers(user: User) -> dict[str, str]:
    """现成的认证头，省掉「注册 + 登录」两步 HTTP 往返。"""
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def cors_origins() -> dict[str, str]:
    """三个固定来源：白名单里的两个（证明逗号真被拆开）、以及一个不在名单上的。

    来源值必须和上面写进环境变量的一致；单独开 fixture 是为了让用例读起来
    是 `cors_origins["blocked"]`，而不是一堆硬编码的 URL。
    """
    return {
        "allowed": CORS_ALLOWED,
        "allowed_alt": CORS_ALLOWED_ALT,
        "blocked": CORS_BLOCKED,
    }


@pytest.fixture
def resume(db: Session, user: User) -> Resume:
    """一份解析好的简历，正文里有数字和技能关键词，mock 打分不会落到最低档。"""
    created = Resume(
        user_id=user.id,
        original_filename="简历.pdf",
        stored_filename="test-stored.pdf",
        file_size=1024,
        char_count=40,
        raw_text="张三 电话 13800000000 本科 熟悉 Python，负责订单系统开发，QPS 提升 3 倍",
    )
    db.add(created)
    db.commit()
    db.refresh(created)
    return created
