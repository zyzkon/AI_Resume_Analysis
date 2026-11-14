"""为压测准备一条可用的数据链：账号 -> 简历 -> 一条跑完的分析。

为什么单独一个脚本：
locust 的每个虚拟用户 on_start 都要登录、都要找一个 analysis_id 来轮询。
如果让 50 个用户各自去上传简历、各自等分析跑完，光是准备阶段就会把数据搞乱
（几百份重复简历），而且测出来的 QPS 里混着准备动作。

所以先固定一条数据，压测时所有用户共享它 —— 这也是真实场景：
一份简历被反复查看，分析记录被反复轮询。

用法：
    python tools/prepare_load_test.py
输出（可直接 export）：
    export LOAD_EMAIL=... LOAD_PASSWORD=... LOAD_RESUME_ID=... LOAD_ANALYSIS_ID=...
"""

import os
import sys
import tempfile
import time

import requests
from reportlab.pdfgen import canvas

BASE = os.getenv("LOAD_BASE_URL", "http://127.0.0.1:8080/api")
EMAIL = os.getenv("LOAD_EMAIL", "loadtest@example.com")
PASSWORD = os.getenv("LOAD_PASSWORD", "loadtest-123456")

RESUME_LINES = [
    "Li Si",
    "Backend Engineer / Full-stack",
    "Email: lisi@example.com  |  GitHub: github.com/example",
    "",
    "SUMMARY",
    "Four years building Python services. Comfortable with FastAPI, Celery,",
    "MySQL and Redis, plus a Vue 3 front end for internal tools.",
    "",
    "SKILLS",
    "Python, FastAPI, SQLAlchemy, MySQL, Redis, Celery, Docker, Vue 3, pytest",
    "",
    "EXPERIENCE",
    "2022-2026 Acme Corp - Backend Engineer",
    "- Designed an async analysis pipeline with Celery and Redis.",
    "- Cut API p95 from 1.2s to 90ms by moving LLM calls to workers.",
    "- Wrote 120+ pytest cases covering auth, upload and task retries.",
    "",
    "PROJECTS",
    "Resume Analyzer: FastAPI + Celery + MySQL, 14 endpoints, Docker Compose.",
    "",
    "EDUCATION",
    "BSc Computer Science, 2022",
]


def make_pdf() -> str:
    """现场生成一份带文字层的 PDF，不往仓库塞二进制样本。"""
    path = os.path.join(tempfile.gettempdir(), "loadtest-resume.pdf")
    c = canvas.Canvas(path)
    y = 780
    for line in RESUME_LINES:
        c.drawString(60, y, line)
        y -= 20
    c.save()
    return path


def main() -> int:
    s = requests.Session()
    # 本机常开全局代理时，requests 会把 127.0.0.1 也丢给代理，
    # 表现是 ReadTimeout 或者连到代理自己的端口（127.0.0.1:5xxxx）。
    # trust_env=False 让这个会话彻底不看 HTTP_PROXY / NO_PROXY
    s.trust_env = False

    # 1. 注册。已存在就忽略 409 —— 脚本要能反复跑
    r = s.post(f"{BASE}/auth/register", json={"email": EMAIL, "password": PASSWORD}, timeout=10)
    print(f"register: {r.status_code}")
    if r.status_code not in (201, 409):
        print(r.text[:300])
        return 1

    # 2. 登录
    r = s.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=10)
    if r.status_code != 200:
        print(f"login 失败: {r.status_code} {r.text[:300]}")
        return 1
    s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    print("login: 200")

    # 3. 复用已有简历，没有再上传。避免反复跑脚本堆出一堆简历
    items = s.get(f"{BASE}/resumes?page=1&size=50", timeout=10).json()["items"]
    if items:
        resume_id = items[0]["id"]
        print(f"resume: 复用已有 id={resume_id}")
    else:
        pdf = make_pdf()
        with open(pdf, "rb") as f:
            r = s.post(f"{BASE}/resumes", files={"file": ("loadtest-resume.pdf", f, "application/pdf")}, timeout=30)
        os.remove(pdf)
        if r.status_code != 201:
            print(f"上传失败: {r.status_code} {r.text[:300]}")
            return 1
        resume_id = r.json()["id"]
        print(f"resume: 新上传 id={resume_id}（解析 {r.json()['char_count']} 字）")

    # 4. 找一条已完成的分析给轮询用；没有就提交一条并等它跑完
    analyses = s.get(f"{BASE}/resumes/{resume_id}/analyses?page=1&size=50", timeout=10).json()["items"]
    done = [a for a in analyses if a["status"] == "done"]
    if done:
        analysis_id = done[0]["id"]
        print(f"analysis: 复用已有 id={analysis_id}")
    else:
        r = s.post(f"{BASE}/resumes/{resume_id}/analyses", json={"job_description": None}, timeout=10)
        analysis_id = r.json()["id"]
        for _ in range(60):
            detail = s.get(f"{BASE}/analyses/{analysis_id}", timeout=10).json()
            if detail["status"] in ("done", "failed"):
                break
            time.sleep(0.5)
        print(f"analysis: 新建 id={analysis_id} 状态={detail['status']}")

    print()
    print(f"export LOAD_EMAIL={EMAIL} LOAD_PASSWORD={PASSWORD} LOAD_RESUME_ID={resume_id} LOAD_ANALYSIS_ID={analysis_id}")
    # token 一起带出去：压测时所有虚拟用户共享同一个 token，
    # 否则 200 个用户同时 on_start 登录，bcrypt 会把 CPU 先烧掉，
    # 测出来的就不是业务接口容量，而是密码哈希的吞吐
    print(f"export LOAD_TOKEN={s.headers['Authorization'].split(' ', 1)[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
