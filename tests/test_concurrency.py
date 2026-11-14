"""并发闸门的用例。

钉的是一个真出过事的东西：200 并发下 12% 请求 500。原因不是「连接池设小了」
—— 把池从 30 加到 40 之后失败率反而涨到 12%。真正的机制是：

    FastAPI 处理「同步依赖 + 同步路由」的请求时，分多次进线程池
    （get_current_user 一次、路由函数一次、依赖清理再一次），数据库连接
    在第一次查库时拿到、到依赖清理才归还，中间那几个间隙里线程已经归还了
    但连接还在手里。于是在途请求（占着连接）可以多于线程数，
    池被这些「正在等线程」的请求抽干，线程又被「正在等连接」的请求占满。

所以限流的对象必须是「请求数」，而不是线程数。下面这几条用例守的就是这个
闸门本身的行为：上限是否真的生效、探活是否被豁免、配置与启动装配是否一致。
"""

from __future__ import annotations

import anyio
import pytest
from pydantic import ValidationError
from starlette.middleware.cors import CORSMiddleware

from app.core.concurrency import ConcurrencyGate
from app.core.config import Settings, settings
from app.main import app


def _settings(**overrides: object) -> Settings:
    """不读 .env 的配置，只用于验证校验器（同 tests/test_cors.py 的做法）。"""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        SECRET_KEY="test-secret-key-0123456789abcdefgh",
        **overrides,
    )


async def _drive(gate: ConcurrencyGate, path: str) -> None:
    """模拟一个请求走完闸门。闸门只读 scope 的 type / path，其余字段给最简值。"""

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        return None

    scope = {"type": "http", "path": path, "method": "GET", "headers": []}
    await gate(scope, receive, send)  # type: ignore[arg-type]


def test_gate_caps_concurrent_requests() -> None:
    """同时打进来 20 个，闸门里最多 3 个，而且都跑完不丢。

    只断言 peak <= 3 太松（把 limit 写成 0 会死锁、写成 100 也能过一半），
    所以同时断言 peak == 3：它必须真的用满额度，否则等于自废武功。
    """
    state = {"now": 0, "peak": 0, "done": 0}

    async def handler(scope: dict[str, object], receive: object, send: object) -> None:
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await anyio.sleep(0.01)
        state["now"] -= 1
        state["done"] += 1

    gate = ConcurrencyGate(handler, limit=3)  # type: ignore[arg-type]

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            for _ in range(20):
                tg.start_soon(_drive, gate, "/resumes")

    anyio.run(main)

    assert state["peak"] == 3
    assert state["done"] == 20
    assert state["now"] == 0  # 名额全部归还，没有泄漏
    assert gate.peak == 3


def test_health_is_not_queued_behind_business_requests() -> None:
    """闸门占满时 /health 仍然立刻返回。

    这条不是小事：探活被排队的话，docker healthcheck 3 秒超时一到容器就被
    标成 unhealthy 摘走流量 —— 服务只是忙，不是死。压测时真实发生过一次。
    """
    order: list[str] = []

    async def handler(scope: dict[str, object], receive: object, send: object) -> None:
        if scope["path"] == "/health":
            order.append("health")
            return
        order.append("slow-begin")
        await anyio.sleep(0.2)
        order.append("slow-end")

    gate = ConcurrencyGate(handler, limit=1)  # type: ignore[arg-type]

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_drive, gate, "/resumes")  # 占住唯一的坑
            await anyio.sleep(0.05)                  # 等它确实进到 handler 里
            await _drive(gate, "/health")            # 不该等那 0.2 秒
            order.append("health-returned")

    anyio.run(main)

    assert order == ["slow-begin", "health", "health-returned", "slow-end"]


def test_gate_wider_than_pool_is_rejected() -> None:
    """闸门比连接池还宽 = 闸门形同虚设，启动时就该拒绝。"""
    with pytest.raises(ValidationError, match="超过"):
        _settings(MAX_CONCURRENT_REQUESTS=999)


def test_gate_limit_defaults_below_pool() -> None:
    """默认值本身要满足「闸门 ≤ 线程 < 连接池」，否则改完默认值就没人守了。"""
    s = _settings()
    assert s.MAX_CONCURRENT_REQUESTS <= s.db_max_connections


def test_startup_wires_gate_with_configured_limit() -> None:
    """装配层：闸门存在、上限来自配置，且装在 CORS 里面。

    `user_middleware` 的顺序是「后添加的更靠外」，CORS 在闸门之外意味着
    预检请求根本进不了闸门，不会白占名额。
    """
    classes = [m.cls for m in app.user_middleware]
    assert ConcurrencyGate in classes
    assert classes.index(CORSMiddleware) < classes.index(ConcurrencyGate)

    gate = next(m for m in app.user_middleware if m.cls is ConcurrencyGate)
    assert gate.kwargs["limit"] == settings.MAX_CONCURRENT_REQUESTS
