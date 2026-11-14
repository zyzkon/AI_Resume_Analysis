"""一次性灌 N 个分析任务，用来测 worker 的消化速度。

测的不是接口 QPS，而是「任务/分钟」—— 异步架构里真正该看的数字：
接口 202 返回得再快，worker 处理不过来也只是一堆 pending。

用法：
    export LOAD_TOKEN=... LOAD_RESUME_ID=2
    python tools/burst_submit.py 200
输出：
    submitted=200 elapsed=3.2s max_id=731
"""

import concurrent.futures
import os
import sys
import time

import requests

BASE = os.getenv("LOAD_BASE_URL", "http://127.0.0.1:8080/api")
TOKEN = os.getenv("LOAD_TOKEN", "")
RESUME_ID = os.getenv("LOAD_RESUME_ID", "")

# 顺序提交的话，瓶颈是 HTTP 往返（每次约 50ms），worker 早就处理完了，
# 测出来的是「客户端提交速度」而不是「worker 消化速度」。
# 所以用线程池并发提交，把漏斗先灌满
CONCURRENCY = 20


def submit_one(_: int) -> int | None:
    s = requests.Session()
    s.trust_env = False
    s.headers["Authorization"] = f"Bearer {TOKEN}"
    r = s.post(f"{BASE}/resumes/{RESUME_ID}/analyses", json={"job_description": None}, timeout=30)
    return r.json()["id"] if r.status_code == 202 else None


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    if not TOKEN or not RESUME_ID:
        print("需要 LOAD_TOKEN 和 LOAD_RESUME_ID，先跑 prepare_load_test.py")
        return 1

    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        ids = [i for i in pool.map(submit_one, range(n)) if i]
    elapsed = time.perf_counter() - started

    print(f"submitted={len(ids)} failed={n - len(ids)} elapsed={elapsed:.1f}s max_id={max(ids) if ids else 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
