"""压测脚本：按真实用户的行为比例压 API。

三个数字要分开看，所以每个接口是独立的 task，locust 会按 name 分组统计：
    轮询 GET /analyses/{id}      —— 前端每 2 秒一次，整站最高频
    列表 GET /resumes            —— 进首页就查一次
    提交 POST /resumes/{id}/analyses —— 202 路径，验证队列投递

用法（先跑 tools/prepare_load_test.py 拿到 id）：
    export LOAD_ANALYSIS_ID=1 LOAD_RESUME_ID=1
    locust -f tools/locustfile.py --headless -u 50 -r 10 -t 60s \
           --host http://127.0.0.1:8080/api --only-summary

为什么必须 LLM_PROVIDER=mock：
真实模型一次 30~90 秒，压测时会瞬间堆几百个任务，测出来的不是你的系统吞吐，
而是对方 API 的排队时间，同时还烧钱。mock 固定几十毫秒，才能把系统开销单独拎出来。
"""

import os
import random

from locust import HttpUser, between, tag, task

EMAIL = os.getenv("LOAD_EMAIL", "loadtest@example.com")
PASSWORD = os.getenv("LOAD_PASSWORD", "loadtest-123456")
RESUME_ID = os.getenv("LOAD_RESUME_ID", "")
ANALYSIS_ID = os.getenv("LOAD_ANALYSIS_ID", "")

JD_SAMPLES = [
    None,
    "后端工程师，要求熟悉 Python / FastAPI / MySQL，有异步任务经验优先",
    "全栈工程师，Vue 3 + FastAPI，负责内部工具的开发与维护",
]


class ResumeUser(HttpUser):
    """一个普通用户：登录后主要在看分析结果，偶尔提交新的分析。

    wait_time 给的很短（0.1~0.5 秒）是为了测容量上限，而不是模拟真人节奏。
    要测"真实用户量下的负载"，把这里改成 between(2, 5) 再跑。
    """

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        token = os.getenv("LOAD_TOKEN", "")
        if token:
            # 预取的 token 优先。200 个用户同时 on_start 登录时，bcrypt 单次约 0.8 秒，
            # 会先把 api 的线程池占死几秒，业务接口跟着一起超时 ——
            # 那样测出来的是密码哈希的吞吐，不是接口容量。
            # 登录接口的容量单独用 --tags 测（见 README）。
            self.client.headers["Authorization"] = f"Bearer {token}"
        else:
            r = self.client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}, name="/auth/login")
            if r.status_code != 200:
                # 登录失败时继续压没有意义，直接把这个虚拟用户踢掉
                raise RuntimeError(f"压测账号登录失败: {r.status_code} {r.text[:200]}")
            self.client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

        self.resume_id = RESUME_ID
        self.analysis_id = ANALYSIS_ID

        # 没给 id 就自己找一条，保证脚本单独也能跑
        if not self.resume_id:
            items = self.client.get("/resumes?page=1&size=1", name="/resumes").json()["items"]
            if items:
                self.resume_id = str(items[0]["id"])
        if not self.analysis_id and self.resume_id:
            items = self.client.get(
                f"/resumes/{self.resume_id}/analyses?page=1&size=1",
                name="/resumes/{id}/analyses",
            ).json()["items"]
            if items:
                self.analysis_id = str(items[0]["id"])

    @tag("poll")
    @task(10)
    def poll_analysis(self) -> None:
        """权重 10：这是前端轮询打的接口，也是连接池最先撑不住的地方。"""
        if not self.analysis_id:
            return
        self.client.get(f"/analyses/{self.analysis_id}", name="/analyses/{id}")

    @tag("list")
    @task(4)
    def list_resumes(self) -> None:
        self.client.get("/resumes?page=1&size=20", name="/resumes")

    @tag("trend")
    @task(2)
    def trend(self) -> None:
        if not self.resume_id:
            return
        self.client.get(f"/resumes/{self.resume_id}/analyses/trend?limit=50", name="/resumes/{id}/analyses/trend")

    @tag("submit")
    @task(1)
    def submit_analysis(self) -> None:
        """权重 1：写路径。每个请求都会落一条 pending 记录，压太狠会把库灌满。"""
        if not self.resume_id:
            return
        self.client.post(
            f"/resumes/{self.resume_id}/analyses",
            json={"job_description": random.choice(JD_SAMPLES)},
            name="/resumes/{id}/analyses [POST]",
        )

    @tag("login")
    @task(1)
    def login(self) -> None:
        """单独压登录用（--tags login）。

        bcrypt 校验是纯 CPU 活儿，一次约 0.3~0.8 秒，所以登录接口的吞吐
        和读接口完全不是一个量级。混在业务压测里会污染数据，必须分开测。
        """
        self.client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}, name="/auth/login")
