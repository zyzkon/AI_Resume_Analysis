"""请求级并发闸门：超出的请求在事件循环里排队，而不是进线程池互相堵死。

这个中间件解决的是一个真实发生过的死锁（2026-11 压测，200 并发下 12% 请求 500）：

FastAPI 处理一个「同步依赖 + 同步路由」的请求时，会分**多次**进线程池 ——
`get_current_user` 一次、路由函数一次、依赖清理再一次。数据库连接在第一
次查库时拿到，之后一直held着，直到依赖清理才归还，也就是说：

    第一阶段结束 → 线程归还了，但连接没归还
    要进第二阶段 → 得再借一个线程；借不到就等，而这时连接还在手里

于是出现双向等待：连接被「正在等线程」的请求占着，线程被「正在等连接」的
请求占着，两边都不动，直到 pool_timeout 到期抛 TimeoutError。

`thread_pool == pool_size` 这种对齐**并不能**解决它，因为占着连接的请求
可能比线程还多。真正要限的是「同时在处理的请求数」——在这批请求进入
线程池之前就把它们挡住，让在途请求数 ≤ 线程数 ≤ 连接数，等式才成立。

放这里而不是放路由里：它是全局不变量，不该由每个路由自己记得。
"""

from collections.abc import Awaitable, Callable
from typing import Any

import anyio

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# 探活不排队。它不碰数据库、几百微秒就返回，但如果被排在 30 个业务请求
# 后面，docker healthcheck 的 3 秒超时一到，容器就被标成 unhealthy 摘走流量 ——
# 服务只是忙，不是死，这两件事必须能区分开
EXEMPT_PATHS = frozenset({"/health"})


class ConcurrencyGate:
    """上限由 MAX_CONCURRENT_REQUESTS 决定，和线程池容量是同一个数。"""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit
        self._semaphore = anyio.Semaphore(limit)
        # 三个计数只用于诊断（/debug 探针和压测对照），不参与限流决策
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # lifespan / websocket 没有"请求"的概念，直接放行
        if scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        async with self._semaphore:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                # 整个请求（含响应发送）都在闸门内。慢客户端会多占一个名额，
                # 这是刻意的取舍：宁可占名额，也不要在发送期间把连接还回去
                # 又让下一个请求挤进来
                await self.app(scope, receive, send)
            finally:
                self.in_flight -= 1
