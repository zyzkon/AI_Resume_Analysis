"""Celery 应用实例：只负责"配置"，不写业务。

为什么不把任务函数也放在这里：
`celery -A app.workers.celery_app:celery_app` 会用这个模块名做 app 的入口，
如果把任务写在同一文件里，每次改任务逻辑都得重启 worker；
更重要的是，celery_app 必须能在"没有数据库连接"的情况下被 import
（比如 `celery inspect`、`celery purge` 这些命令根本不需要任务代码），
业务逻辑塞进来会让这些命令也开始连 MySQL。

配置里每一条都有代价，写在后面的注释里，不要随手删。
"""

from celery import Celery
from celery.signals import worker_process_init

from app.core.config import settings
from app.db.session import engine

# 单次任务允许耗时多久：模型单次请求上限 × (1 + 重试次数)，再留 30 秒余量。
# 这个值必须大于 services/llm.py 内部重试的总时长，否则会出现
# "任务被 Celery 杀掉，但模型其实还在正常重试"的误杀。
# settings.LLM_TIMEOUT 是 float，配置里要整数秒
_LLM_BUDGET = int(settings.LLM_TIMEOUT * (settings.LLM_MAX_RETRIES + 1))
SOFT_TIME_LIMIT = _LLM_BUDGET + 30
HARD_TIME_LIMIT = SOFT_TIME_LIMIT + 30

celery_app = Celery(
    "resume_analyzer",
    # broker 和 backend 用同一个 Redis 实例、同一个 db：
    # broker 的队列键叫 celery，backend 的结果键叫 celery-task-meta-*，前缀不同不会撞。
    # 真要在生产上隔离，就把 backend 换成 redis://host:6379/1
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    # 显式列出任务模块。不靠 autodiscover，因为它按包路径猜文件名（找 tasks.py），
    # 一旦文件改名就静默漏注册，报错是运行时"未注册的任务"，很难查
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    # ---- 序列化：只收 json ----
    # 默认还有 pickle，而 pickle 反序列化等于在本机执行任意代码。
    # broker 一旦被外人写入消息，pickle 就是后门，必须关掉
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # ---- 时区 ----
    # 全程 UTC，和模型里的 created_at / finished_at 保持一致。
    # 混用本地时会算出负的排队时间
    timezone="UTC",
    enable_utc=True,
    # ---- 可靠性 ----
    # acks_late：任务真正跑完才 ack。默认是"收到就 ack"，worker 跑到一半被 kill
    # 消息就永久丢了，记录会永远卡在 running。代价是可能重复投递 —— 靠任务的幂等兜住
    task_acks_late=True,
    # worker 进程被 OOM 杀掉时把消息退回队列，而不是当成功处理
    task_reject_on_worker_lost=True,
    # 一次只预取 1 个任务。默认是 4，意味着一台机器上排 4 个长任务，
    # 后面的 worker 想帮忙也拿不到，队列尾部的用户白等
    worker_prefetch_multiplier=1,
    # ---- 超时 ----
    # soft 先到：抛出 SoftTimeLimitExceeded，任务可以捕获它，把记录标成 failed 再退出，
    # 用户看到的是"超时失败"而不是一条永远 running 的僵尸记录。
    # hard 后到：worker 直接杀掉子进程，兜住 "except 里又卡住" 的情况
    task_soft_time_limit=SOFT_TIME_LIMIT,
    task_time_limit=HARD_TIME_LIMIT,
    # Redis 作为 broker 时，消息被取走后会进入"未确认"状态，
    # 超过 visibility_timeout 还没 ack 就自动重新投递（本质是 Redis 没有真正的 ack 语义）。
    # 默认 1 小时，这里压到硬超时 + 1 分钟，让"worker 死了"能更快被发现
    broker_transport_options={"visibility_timeout": HARD_TIME_LIMIT + 60},
    # Redis 还没起来时 worker 不要直接退出，按固定间隔重连。
    # 不加这条，docker 和 worker 同时启动会看到 worker 启动失败
    broker_connection_retry_on_startup=True,
    # 任务结果只在 Redis 里留 1 小时。真实结果早写进 MySQL 了，
    # Redis 里这份只是给 AsyncResult 查状态用的，不留垃圾
    result_expires=3600,
    # 统一队列名，将来要按任务类型分流（快/慢任务分开 worker）时从这里切开
    task_default_queue="analysis",
    # 注意这里没有 task_track_started：任务声明了 ignore_result=True，
    # 结果不写 backend，"开始执行"这个状态也就没地方存。
    # 这不算损失 —— 任务进行到哪一步，MySQL 的 status 列（pending/running/done/failed）
    # 本来就是唯一事实来源，比 Redis 里那份更可靠
)


@worker_process_init.connect
def _reset_db_pool_after_fork(**kwargs) -> None:
    """fork 出来的子进程必须扔掉从父进程继承的数据库连接。

    prefork 模式下，父进程先创建 engine（此时连接池里可能有已建立的 socket），
    fork 出来的子进程会原样继承这些文件描述符。多个进程共用一个 MySQL 连接，
    表现是随机报 "Packet sequence number wrong" 或 "Lost connection during query"，
    而且只在并发时才出现，极难定位。

    close=False 很关键：子进程只丢弃自己对池的引用，不真正关闭底层 socket，
    否则第一个 fork 出来的子进程会把父进程要用的连接关掉。
    """
    engine.dispose(close=False)
