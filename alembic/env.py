"""Alembic 运行环境。

三个关键点：
1. 数据库地址不写在 alembic.ini 里，而是从 app.core.config 的 settings 读，
   保证迁移和应用永远指向同一个库，不会出现"应用连 A、迁移改 B"。
2. 设置 ALEMBIC_DATABASE_URL 环境变量可临时覆盖，用于对着空库生成基线、
   或者给预发库单独跑一次迁移。
3. target_metadata 必须挂 Base.metadata，且在此之前 import 全部模型，
   否则 autogenerate 会认为"所有表都该被删掉"。
"""

import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context

from app.core.config import settings
from app.db.session import Base
import app.models  # noqa: F401  导入即把三张表登记进 Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    """迁移连接串：环境变量优先，其次跟着应用配置走。"""
    return os.getenv("ALEMBIC_DATABASE_URL") or settings.DATABASE_URL


def _set_url(url: str) -> None:
    # configparser 会把 % 当插值语法，密码里含 % 时会抛 InterpolationSyntaxError
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))


def run_migrations_offline() -> None:
    """离线模式：只把 SQL 打到标准输出，不连库。适合交给 DBA 审或手工执行。"""
    _set_url(get_url())
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行。用 NullPool，跑完就断，别把连接池留在这个短命进程里。"""
    connectable = create_engine(get_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 类型变化也纳入比对（如 String(32) -> String(64)）。
            # 旧版 alembic 默认关着，只比表名列名，改了长度它当没发生
            compare_type=True,
            # server_default 不比对：MySQL 会把 CURRENT_TIMESTAMP 回读成
            # current_timestamp()，字符串默认值也可能被加上引号，
            # 一比对就永远有差异，生成一堆无意义的迁移
            compare_server_default=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
