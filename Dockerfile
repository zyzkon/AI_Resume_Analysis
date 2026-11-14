# 三个阶段，两个最终产物：
#
#   web-build  ──> web-server   静态站点（Caddy 托管 dist + 反代 /api）
#                        └──> runtime   Python 运行时（api / worker / beat 共用同一个镜像）
#
# api、worker、beat 只差一个启动命令，所以共用一个镜像 —— 这也正是当初把业务
# 抽进 app/services 的回报：任务层不用单独打包。

# ---------- 阶段 1：构建前端 ----------
# 用 slim（Debian / glibc）而不是 alpine：package-lock.json 是在 Windows 上生成的，
# 里面的 rollup / esbuild 是各平台独立的二进制包，alpine 的 musl 版本偶尔装不上，
# 表现是构建时报「Cannot find module @rollup/rollup-linux-x64-musl」
FROM node:22-slim AS web-build

WORKDIR /web

# 先只拷依赖清单：只要 package*.json 没变，这一层就命中缓存，
# 改业务代码不会触发 npm ci 重装（它是最慢的一步）
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./

# 生产环境走同源：请求打 /api，由 Caddy 剥掉前缀转发给 api:8000。
# 必须是构建期注入 —— Vite 在构建时把 import.meta.env 替换成字面量，
# 打包产物里没有"运行时读环境变量"这回事
ENV VITE_API_BASE_URL=/api

RUN npm run build

# ---------- 阶段 2：Python 运行时 ----------
FROM python:3.12-slim AS runtime

# PYTHONUNBUFFERED：日志立刻刷到 stdout，不然 docker logs 是攒一波才出
# PYTHONDONTWRITEBYTECODE：容器里不需要 .pyc，少一层写盘
# TZ=UTC：与服务端存 UTC 的约定一致，混用本地时会算出负的排队时间
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

# 依赖单独一层，改代码不会重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# 不用 root 跑应用。uploads 必须在这里建好并 chown：
# 后面挂的是命名卷，Docker 首次创建卷时会把镜像内该路径的属主复制过去，
# 如果镜像里没有这个目录，卷会以 root 属主出现，应用就写不进文件
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# 单进程即可：同步路由跑在 FastAPI 的线程池里，
# 要加 --workers 就得同时考虑每个进程各持一个连接池
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------- 阶段 3：静态站点 ----------
FROM caddy:2-alpine AS web-server

COPY docker/Caddyfile /etc/caddy/Caddyfile
COPY --from=web-build /web/dist /srv

EXPOSE 80
