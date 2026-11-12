# AI 简历分析

上传一份 PDF / DOCX 简历，调用大模型输出结构化评分：简历完整度、内容质量、岗位匹配度，以及按优先级排序的修改建议。

FastAPI + SQLAlchemy + MySQL + Redis + Celery。上传和查询是同步的，耗时几十秒的模型调用交给独立 worker 异步执行。

> 个人学习项目，接口契约、Prompt 和表结构都还在迭代，不保证向后兼容。

## 实现进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 项目骨架、配置加载、数据库会话、健康检查 | 已完成 |
| 1 | 注册登录、JWT 鉴权、资源归属校验 | 已完成 |
| 2 | 简历上传、文件校验、列表与删除 | 已完成 |
| 3 | PDF / DOCX 文本提取与清洗 | 已完成 |
| 4 | 大模型接入：Prompt、输出校验、重试、mock | 已完成 |
| 5 | 分析接口：提交、轮询、历史列表 | 已完成 |
| 6 | Celery 异步化：202 + 任务队列 + 两层超时 | 已完成 |
| 7 | 历史对比：分数走势与建议差异 | 已完成 |
| 8 | OpenAPI 文档：错误码、示例、401 文案统一 | 已完成 |
| 9 | pytest 测试套件：126 个用例，SQLite + mock，4 秒跑完 | 已完成 |
| 10 | Alembic 数据库迁移：基线 + 版本链，已有数据的库用 stamp 接入 | 已完成 |
| 11 | 僵尸任务清理：beat 每 5 分钟收敛卡在 pending / running 的记录 | 已完成 |
| 12 | CORS 收敛：来源走 `.env` 白名单，方法与请求头按下限放行 | 已完成 |

后端到此为止。**前端还没做** —— 现在能看到的 `/docs` 是 FastAPI 自动生成的调试页，不是产品界面。

## 功能

- 邮箱注册 / 登录，JWT 鉴权（bcrypt 存密码）
- 上传 PDF / DOCX，服务端解析出纯文本入库（扩展名 + 魔数双校验，5 MB 上限）
- 提交分析任务后**立即返回 202**，worker 后台调用模型，前端轮询状态
- 分析结果结构化存储：总分、各栏目检查、建议列表、模型名、Prompt 版本
- 支持传入目标岗位 JD，输出岗位匹配度
- **历史对比**：同一份简历多次分析的分数走势，以及两次分析之间的建议变化（新增 / 已解决 / 仍存在）
- 全部资源按用户隔离，越权访问统一返回 404 而不是 403，不泄露资源是否存在
- 14 个接口的 OpenAPI 文档齐全：每个错误码都有说明和响应示例，`/docs` 可以直接当接口说明书用
- `mypy` 全量通过（35 个文件，开了 `warn_unused_ignores`、`strict_equality` 等严格选项）
- `pytest` 126 个用例全绿：认证、上传五道关、PDF/DOCX 解析、LLM 输出容错、分析状态机、差异计算、僵尸清理、CORS
- 表结构变更走 Alembic 迁移，不再依赖 `create_all`
- 跨域来源走白名单配置，默认只放行本机开发端口

## 技术栈

| 层 | 选型 |
|---|---|
| Web | FastAPI 0.162 · Uvicorn |
| 数据 | SQLAlchemy 2.1 · MySQL 8（utf8mb4）· PyMySQL |
| 队列 | Celery 5.6 · Redis 7+ |
| 校验 | Pydantic 2 · pydantic-settings |
| 模型 | OpenAI 兼容接口（默认 DeepSeek），内置 `mock` 模式可离线跑通全流程 |
| 解析 | pdfplumber · python-docx |
| 迁移 | Alembic（`requirements.txt`，部署时要在应用之外单独跑） |
| 检查 | mypy · pytest（`requirements-dev.txt`，不进生产依赖） |

## 架构

两个进程，一套业务代码：

```mermaid
flowchart TB
    C["客户端 / Swagger"] --> A["API 进程<br/>uvicorn · FastAPI 路由"]
    A -->|"写入 pending / 读状态"| DB[("MySQL")]
    A -->|".delay() 投递"| Q["Redis 队列"]
    Q -->|"worker 取任务"| W["Celery worker"]
    A --> S["app/services<br/>parser · llm · analysis · comparison"]
    W --> S
    S --> DB
    S --> LLM["大模型 API"]
```

`app/services` 被两个进程共同 import，这是把业务从 `tasks.py` 里抽出来的原因：抽完之后任务层只剩 75 行，全是 `SessionLocal()`、`close()` 和捕获超时信号。想批量重跑历史分析，`for` 循环调 `execute_analysis(db, id)` 就行，不用起 Redis。

各层的边界规则：

| 层 | 文件 | 只准做 | 不做 |
|---|---|---|---|
| 路由 | `api/routes/*.py` | 找数据、调服务、写库 | 拼 Prompt、判 `if LLM_PROVIDER` |
| 依赖 | `api/deps.py` | token → User | 任何业务判断 |
| 服务 | `services/*.py` | 纯业务 | 开 Session、碰 HTTP、import Celery |
| 任务 | `workers/tasks.py` | 进程生命周期 | 业务逻辑 |
| 模型 | `models/*.py` | 表结构 | 业务规则 |
| 校验 | `schemas/*.py` | 出入口形状 | 查库 |

## 请求是怎么走的

```
POST /resumes/{id}/analyses
  |
  +-- 1. 写入一条 status=pending 的记录  --------->  MySQL
  +-- 2. run_analysis.delay(id)  ----------------->  Redis 队列
  +-- 3. 立刻返回 202 + status=pending

                    Redis 队列
                        |  worker 取走
                        v
                 UPDATE status=running  ------------->  MySQL
                 调用大模型（10 ~ 90 秒）
                 UPDATE status=done / failed  ------->  MySQL

GET /analyses/{id}
  +-- 读 status（pending / running / done / failed） --->  MySQL
```

两条约定：

1. **消息体里只有 `analysis_id`**，简历正文由 worker 自己查库 —— 两万字的正文不该塞进 Redis。
2. **状态的唯一事实来源是 MySQL 的 `status` 列**，Redis 里不存结果副本，避免两边不一致时不知道该信谁。

## 快速开始

### 1. 前置依赖

- Python 3.12+
- MySQL 8（要求 `utf8mb4`，简历里有 emoji 也不会炸）
- Redis 7+

没有 MySQL / Redis 的话，用 Docker 起一个：

```bash
docker run -d --name mysql -p 3306:3306 \
  -e MYSQL_ROOT_PASSWORD=your-root-password \
  -e MYSQL_DATABASE=resume_db \
  -e MYSQL_USER=resume \
  -e MYSQL_PASSWORD=your-password \
  mysql:8.0 --character-set-server=utf8mb4 --collation-server=utf8mb4_0900_ai_ci

docker run -d --name redis -p 6379:6379 redis:7-alpine
```

不想暴露 3306 的话，仓库里的 `docker-compose.yml` 用的是 3307 端口，`docker compose up -d` 即可。

### 2. 装依赖

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
# Windows PowerShell: copy .env.example .env
```

至少要改这三处：

| 变量 | 说明 |
|---|---|
| `SECRET_KEY` | `python -c "import secrets;print(secrets.token_urlsafe(48))"` 生成 |
| `DATABASE_URL` | 必须是 `mysql+pymysql://` 前缀，密码里的 `@ : /` 要做 URL 编码 |
| `LLM_API_KEY` | 用 `LLM_PROVIDER=mock` 时可以不填 |

### 4. 建库建表

库要先建好（表由迁移脚本创建）。登录 MySQL（`mysql -uroot -p`）后执行：

```sql
CREATE DATABASE resume_db DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'resume'@'%' IDENTIFIED BY 'your-password';
GRANT ALL PRIVILEGES ON resume_db.* TO 'resume'@'%';
```

```bash
python -m alembic upgrade head     # 顺着迁移链建出三张表
python -m alembic current          # 确认版本号是 (head)
```

之后改表也走这套，别再回到 `create_all` —— 它只建不存在的表，模型加了字段它既不补列也不报错，应用直到查那一列才炸。详见 [数据库迁移](#数据库迁移)。

### 5. 启动（两个终端）

```bash
# 终端 1：API
uvicorn app.main:app --reload

# 终端 2：worker + beat（开发期合在一起）
celery -A app.workers.celery_app:celery_app worker -l info -P solo -B
```

Windows 必须加 `-P solo`（或 `-P threads`），Celery 的 prefork 模型在 Windows 上不可用。

`-B` 是内嵌 beat，负责定时投递清理任务（每 5 分钟扫一次僵尸记录）。它只适合单机 —— 多个 worker 各带一个 beat，同一个任务会被调度多次。生产环境要拆成三份：worker 一份、`celery ... beat` 单独一份。

不想跑 beat 也可以，`-B` 去掉即可：代价是硬超时留下的 `running` 记录没人清理，前端会一直转圈。

**只起 API 不起 worker，分析记录会停在 `pending`** —— 这是异步系统的正常表现，不是 bug。一小时后清理任务会把它标成 `failed` 并写明「worker 可能没有运行」，省得用户对着一个转圈的页面干等。

### 6. 试一试

- Swagger：<http://127.0.0.1:8000/docs>，点右上角 Authorize 填 `access_token`
- 命令行：用 `test_main.http`（PyCharm / IntelliJ HTTP Client），按顺序点绿色箭头即可，`token` / `resume_id` / `analysis_id` 会自动串起来

验证链路是否通畅：

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","version":"0.1.0","debug":true}
```

如果本机开了全局代理，curl 访问 127.0.0.1 可能被代理拦成 502，加 `--noproxy '*'`。

## 配置项

全部来自 `.env`，由 `app/core/config.py` 统一加载。必填项缺失时**导入即报错**，不会带着错误配置跑起来。

| 分组 | 变量 | 默认值 | 说明 |
|---|---|---|---|
| 应用 | `APP_NAME` | `Resume Analyzer API` | 显示在 `/docs` 顶部 |
| | `APP_VERSION` | `0.1.0` | 由 `/health` 返回 |
| | `DEBUG` | `true` | 透传到 `/health`，不影响日志级别 |
| 安全 | `SECRET_KEY` | **必填** | JWT 签名密钥，少于 32 字符直接报错 |
| | `ALGORITHM` | `HS256` | |
| | `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | 24 小时 |
| 数据库 | `DATABASE_URL` | 见 `.env.example` | 必须以 `mysql+pymysql://` 开头，有校验器拦 |
| | `DB_ECHO` | `false` | 打开后每条 SQL 打印到控制台 |
| 跨域 | `CORS_ORIGINS` | 本机 `:5173` | 逗号分隔、必须带协议头、不能带路径。留空 = 不允许跨域 |
| 上传 | `UPLOAD_DIR` | `uploads` | 相对路径按项目根目录解析 |
| | `MAX_UPLOAD_MB` | `5` | 边读边累加，超限立刻中断 |
| 队列 | `REDIS_URL` | `redis://localhost:6379/0` | broker 和 backend 共用同一个库 |
| 模型 | `LLM_PROVIDER` | `mock` | `mock` 或 `openai`，Literal 类型，写错启动就报错 |
| | `LLM_API_KEY` | 空 | `LLM_PROVIDER=openai` 时必填 |
| | `LLM_BASE_URL` | `https://api.deepseek.com/v1` | 任何 OpenAI 兼容地址 |
| | `LLM_MODEL` | `deepseek-chat` | |
| | `LLM_TIMEOUT` | `90` | 单次请求超时（秒）。连带决定 Celery 的软/硬超时和僵尸清理阈值，改完必须重启 worker |
| | `LLM_MAX_RETRIES` | `2` | 内部重试次数，共尝试 3 次 |
| | `LLM_TEMPERATURE` | `0.2` | 压低是为了打分可复现 |
| | `LLM_MAX_TOKENS` | `2048` | |
| | `LLM_MAX_INPUT_CHARS` | `20000` | 送进模型前的硬上限，超出截断并追加提示 |

改 `LLM_TIMEOUT` 或 `LLM_MAX_RETRIES` 要注意：Celery 的软超时按 `LLM_TIMEOUT × (MAX_RETRIES + 1) + 30` 算出来，改完不需要手工同步，但**必须重启 worker** 才生效。

## 接真实模型

默认 `LLM_PROVIDER=mock`：不联网、不花钱，结果由正文内容决定，**同样输入永远同样输出**。字段名、取值范围和真实模型完全一致，所以整条链路（含数据库里已有的记录）在切换后都不受影响 —— 这也是阶段 5、6 全程能用 mock 开发的原因。

换真实通道只改配置，代码一行不动：

```bash
LLM_PROVIDER=openai
LLM_API_KEY=sk-xxxxxxxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

任何 OpenAI 兼容接口都能用（DeepSeek / 通义 / Kimi / 本地 vLLM），通常只需要改 `LLM_BASE_URL` 和 `LLM_MODEL`。

配了 `openai` 却没给 key 时，应用会在**启动阶段**就报错，不会等到第一个分析请求打进来才 401 —— 那时候排查很容易怀疑到网络和额度上去。

## 接口一览

| 方法 | 路径 | 说明 | 成功状态码 |
|---|---|---|---|
| GET | `/health` | 健康检查 | 200 |
| POST | `/auth/register` | 注册 | 201 |
| POST | `/auth/login` | 登录（JSON） | 200 |
| POST | `/auth/token` | 登录（OAuth2 表单，`username` 填邮箱） | 200 |
| GET | `/auth/me` | 当前用户 | 200 |
| POST | `/resumes` | 上传并解析简历 | 201 |
| GET | `/resumes` | 我的简历列表 | 200 |
| GET | `/resumes/{id}` | 简历详情（正文前 500 字） | 200 |
| DELETE | `/resumes/{id}` | 删除简历（级联删文件与分析记录） | 204 |
| POST | `/resumes/{id}/analyses` | 提交分析（异步） | **202** |
| GET | `/resumes/{id}/analyses` | 某份简历的分析历史 | 200 |
| GET | `/resumes/{id}/analyses/trend` | 分数走势（折线图数据） | 200 |
| GET | `/resumes/{id}/analyses/compare` | 对比两次分析 | 200 |
| GET | `/analyses/{id}` | 分析结果 / 轮询状态 | 200 |

除 `/health`、`/auth/register`、两个登录接口外，其余都需要 `Authorization: Bearer <token>`。

### 状态码约定

| 码 | 场景 |
|---|---|
| 400 | 简历正文为空，无法分析；对比时两个 id 相同 |
| 401 | 未带 token、token 无效、密码错误 |
| 404 | 资源不存在**或不属于当前用户**（统一按不存在处理） |
| 409 | 邮箱已被注册；拿未完成（pending / running / failed）的分析做对比 |
| 413 | 上传文件超过 5 MB |
| 415 | 扩展名或文件魔数不是 PDF / DOCX |
| 422 | 请求体字段校验失败 |
| 503 | 任务队列不可用（Redis 挂了），可退避重试 |

模型调用失败**不会**变成 HTTP 错误码。分析已经异步化了，请求早就返回 202，失败只能体现在 `status=failed` 和 `error_message` 里 —— 客户端该盯的是这两个字段，不是 HTTP 状态码。

## 目录结构

```
alembic.ini                 迁移配置（不含数据库地址，故意留空）
alembic/
├── env.py                  迁移环境：连接串取自 settings，target_metadata 挂 Base
├── script.py.mako          新迁移文件的模板
├── README                  常用命令与两个坑，改表前先看一眼
└── versions/
    └── 20261112_d2bcc03fb86a_baseline.py   基线：create_all 时代的三张表
app/
├── main.py               FastAPI 实例、中间件、路由注册
├── api/
│   ├── deps.py           get_current_user：JWT -> User
│   ├── openapi.py        tag 说明与标准错误响应（文档素材，不含逻辑）
│   └── routes/           auth / resumes / analyses 三组端点，14 个接口
├── core/
│   ├── config.py         Settings，环境变量的唯一来源
│   └── security.py       密码哈希、JWT 签发与校验
├── db/
│   ├── session.py        engine、连接池、SessionLocal
│   └── init_db.py        开发兜底建表，正式建表走 Alembic
├── models/               SQLAlchemy 表定义
│   ├── user.py           users：邮箱唯一，密码只存 bcrypt 哈希
│   ├── resume.py         resumes：原文件名 + UUID 落盘名 + 解析后的正文
│   └── analysis.py       analyses：状态机、result JSON、模型与 Prompt 版本
├── schemas/              Pydantic 入参 / 出参
│   ├── user.py           注册、登录、token、用户信息
│   ├── resume.py         上传响应、列表、详情（正文截前 500 字）
│   ├── analysis.py       分析入参、结果结构、列表与详情
│   ├── comparison.py     对比与走势的出参模型
│   └── system.py         健康检查出参
├── services/             业务逻辑，不认识 FastAPI 也不认识 Celery
│   ├── parser.py         PDF / DOCX -> 纯文本
│   ├── prompts.py        Prompt 与版本号
│   ├── llm.py            调模型、解析与校验输出、重试
│   ├── analysis.py       一次分析的完整编排，可同步调用
│   ├── comparison.py     两次分析的差异计算，纯函数
│   └── maintenance.py    僵尸记录清理，一条带条件的 UPDATE
└── workers/
    ├── celery_app.py     Celery 实例与配置、超时与清理阈值、beat 调度表
    └── tasks.py          任务薄壳：Session 生命周期 + 超时信号 + 定时任务注册
```

分层的意义：`services/` 里的东西既能被路由调用，也能被 worker 调用，还能被脚本和单元测试直接调用 —— 想批量重跑历史分析，`for` 循环调 `execute_analysis(db, id)` 就行，不用起 Redis。

## 数据模型

三张表，全部 `ON DELETE CASCADE`，删用户会连带删简历和分析：

```mermaid
erDiagram
    users ||--o{ resumes : "拥有"
    users ||--o{ analyses : "发起"
    resumes ||--o{ analyses : "关联"
```

| 表 | 关键字段 | 说明 |
|---|---|---|
| `users` | `email`（唯一索引）、`hashed_password`、`is_active` | 只存认证必需的信息，任何情况下不落明文密码 |
| `resumes` | `original_filename`、`stored_filename`（UUID，唯一）、`raw_text`、`char_count` | 文件名和磁盘名解耦，避免同名覆盖和 `../` 穿越 |
| `analyses` | `status`、`result`（JSON）、`match_score`、`model_name`、`prompt_version`、三个时间戳 | `created_at` / `started_at` / `finished_at` 之差分别是排队耗时和执行耗时 |

几个刻意的冗余：

- **`raw_text` 单独存一份**。分析时直接查库取用，不必每次重新解析 PDF —— 解析一份 PDF 要一两秒，而这份简历可能被分析几十次。
- **`result` 用 JSON 列而不是拆成十几个字段**。Prompt 的输出结构还在迭代，拆成列意味着每改一次 Prompt 就要改一次表结构；JSON 列只改 Pydantic 模型。
- **`match_score` 从 JSON 里单独抽成一列**。列表页要按匹配度排序，而 SQLite 的 JSON 函数能力有限、跨库也不通用，冗余一个 int 更省事。
- **`analyses.user_id` 冗余一列**。通过 `resume_id` 能关联出用户，但「查我的所有分析记录」是最高频查询，冗余能省掉一次 join；简历被删后记录也还能独立追溯。
- **`prompt_version` 在提交时就写**。等 worker 成功后再补的话，失败记录会落成建表时的默认值 `v1` —— 一个这次运行里从未存在过的版本号。

时间字段全存 UTC。混用本地时会算出负的排队时间。

## 数据库迁移

`create_all` 只能建「不存在」的表。表一旦存在，它就整张跳过 —— 不检查、不提醒、不报错。所以在模型里加一列再跑一次，输出照样是「建表完成」，应用也能启动，直到第一个请求查到那一列才报 `Unknown column`。

Alembic 把每次结构改动写成一个文件，文件之间用 `down_revision` 串成版本链，库里一张 `alembic_version` 单行表记着「执行到哪一版」。`upgrade head` 读这个指针，把后面没跑过的脚本依次执行完。

```bash
python -m alembic current                    # 当前版本
python -m alembic history                    # 版本链
python -m alembic revision --autogenerate -m "加 xxx 字段"
python -m alembic upgrade head
python -m alembic downgrade -1               # 回退一格
python -m alembic check                      # 库结构与模型是否一致，适合放 CI
```

改表的固定流程：**改模型 → `revision --autogenerate` → 逐行审脚本 → `upgrade head` → 跑 pytest**。第二步的产出永远是草稿：改列名会被渲染成 `drop_column` + `add_column`，脚本执行成功、版本号也进了，只是那一列的数据没了。要手工改成 `op.alter_column('表', '旧名', new_column_name='新名')`。

三个约定：

- **连接串不写进 `alembic.ini`**。`env.py` 从 `settings.DATABASE_URL` 取，保证迁移和应用指向同一个库；临时换库用环境变量 `ALEMBIC_DATABASE_URL` 覆盖
- **一个迁移只做一件事**。MySQL 的 DDL 不能回滚，一个脚本里改三张表、第二条失败时，第一条已经生效但版本号没更新，重跑直接报「列已存在」
- **不在应用启动时 upgrade**。多 worker / 多实例同时起会并发执行同一批 DDL。迁移是部署流程里独立的一步

**已有数据的库怎么接入**：本项目这三张表是 `create_all` 建的，直接跑 baseline 会因「表已存在」失败。做法是先对着空库 `--autogenerate` 生成 baseline（描述现状），再对真实库执行 `python -m alembic stamp head` —— 它只把版本号写进 `alembic_version`，不执行任何 DDL。这一步做完，库和迁移链才算对上账，之后的版本才是真正的 `upgrade`。

**测试不用迁移**。`tests/conftest.py` 仍然是每用例 `create_all` / `drop_all`：内存 SQLite 上建三张表只要几毫秒，跑一串迁移纯属加负担，而且脚本里的 MySQL 语法在 SQLite 上本来就跑不通。代价是「模型改了、迁移忘了写」时测试照样全绿，这个洞用 `alembic check` 堵。

## 关键设计

**异步而不丢任务**：提交时先落一条 `pending` 记录再 `.delay()`。顺序反过来的话，worker 可能在记录还不存在时就开始跑。任务开了 `acks_late`，worker 中途被杀消息会重新投递，所以 `execute_analysis` 里做了幂等判断（已是 `done` 就跳过）。

**两层超时**：Celery 的 soft limit 300 秒先抛 `SoftTimeLimitExceeded`，任务捕获后把记录标成 `failed` 再退出；hard limit 330 秒由 worker 直接杀进程，兜住「异常处理里又卡住」的情况。这两个值必须大于 `LLM_TIMEOUT × (LLM_MAX_RETRIES + 1)`，否则会误杀还在正常重试的请求。

**硬超时之后的僵尸记录**：hard limit 是直接杀进程，Python 来不及执行任何收尾代码，记录会留在 `running`；worker 被 OOM 或 `kill -9` 同理；broker 丢消息（Redis 没开持久化又重启过）则会让记录留在 `pending`。这三种情况 `execute_analysis` 都管不到，所以另有一个 beat 定时任务，每 5 分钟把「超过 450 秒还在 running」和「超过 1 小时还在 pending」的记录收敛成 `failed` 并写明原因。

阈值的取法有讲究：running 的判定必须明显大于硬超时（`HARD_TIME_LIMIT + 120`），否则一个本就快跑完的任务会被误标 `failed`，随后又自己写成 `done` —— 用户看到状态在 `running / failed / done` 之间来回跳，比卡住更让人困惑。pending 给足一小时，因为「worker 暂时没起」和「消息丢了」从数据库里看不出区别，只能靠等。

清理只做收敛、不做重试。后台偷偷重跑会烧 token，而且用户已经等了十几分钟，再让他等第二轮体验更差——要不要重来应该由用户自己决定。另外它天然幂等：`WHERE status == 'running'` 保证了第二轮 beat 扫到同一条记录时什么都没得改。

**连接池**：`pool_pre_ping` + `pool_recycle=3600` 应付 MySQL 的 `wait_timeout`；prefork 模式下子进程会在 `worker_process_init` 里 `engine.dispose(close=False)`，否则多个进程共用同一个 TCP 连接，会出现只在高并发下才犯的「Packet sequence number wrong」。

**上传六步**：扩展名 → 累加大小 → 魔数 → 落盘 → 解析 → 写库。后三步任一失败都要删掉已落盘的文件，否则磁盘会被垃圾文件慢慢堆满。

**输出容错分层**：数值越界钳制（105 → 100）、枚举拼错归一化（`urgent` → `high`）、列表过长截断，只有「不是 JSON」「缺必填字段」才触发重试 —— 目的是把重试次数压到最低，每次重试都在烧 token。

**建议的模糊匹配**：对比两次分析时，判断「这条建议上次也有」用的是 `difflib.SequenceMatcher` 的字符级相似度，阈值 0.55。去掉标点和空白后比较，因为模型两次输出的标点几乎不可能一致。0.55 这个位置：同一个问题改了措辞通常落在 0.6~0.8，话题相近但内容不同的两个问题通常在 0.4 以下。选字符级而不是分词，是因为中文没有空格，而引入分词库只为做一次比较并不划算。

**对比不落库**：`trend` 和 `compare` 完全由已有的 `result` JSON 现算，不新增表、不缓存。代价是每次请求都要把 JSON 反序列化再校验一遍，收益是改算法时历史数据自动跟上 —— 如果把 diff 结果存表，算法一升级，旧记录就永远是新旧混杂的。目前一份简历几十次分析的规模，现算的开销可以忽略。

**为什么 409 而不是 400**：拿 `pending` 的分析做对比，请求本身没写错，是资源当前状态不允许这个操作。前端拿到 409 应该提示「等分析跑完再看」，拿到 400 则会以为参数填错了。

**错误码进文档**：FastAPI 默认只把成功码和 422 写进 OpenAPI，客户端想知道「什么时候返回 409」只能翻源码。`api/openapi.py` 把每个错误码的描述和响应示例集中定义成常量，路由里 `responses={**AUTH_REQUIRED, 404: NOT_FOUND_RESUME}` 引用 —— 14 个端点共用一份措辞，不会出现「这个接口写『无效凭证』、那个写『认证失败』」的漂移。422 刻意不覆盖，框架自动生成的那份带着字段级的 `loc` / `msg` / `type`，比手写的示例信息量大。

**401 文案统一在中文**：`OAuth2PasswordBearer` 默认会在「没带 Authorization 头」时自己抛 401，文案是写死的英文 `Not authenticated`。传 `auto_error=False` 把这一步收回 `get_current_user` 里，全站 401 只有三种文案（未提供身份凭证 / 无效的凭证 / 登录已过期），前端不用为一条英文单独写判断。

**两层重试不能相乘**：`llm.py` 内部最多尝试 3 次（`LLM_MAX_RETRIES=2`）。如果 Celery 的 `max_retries` 也开成 3，一次超时会变成 3×3=9 次请求，用户等 8 分钟才看到失败，token 也照烧。所以任务层设成 `max_retries=0` —— 真要重试应该由用户主动点一次（新建一条记录），而不是系统在后台偷偷重来。

**重复投递是常态**：`acks_late` 换来的是「worker 崩溃不丢任务」，代价是同一条消息可能被投递两次。所以 `execute_analysis` 开头就判「已是终态就跳过」，而分析本身没有副作用（不扣钱、不发通知），万一真跑两遍，后一次覆盖前一次，结果仍然自洽。

**先删库再删文件**：反过来如果文件删了但 commit 失败，库里会留一条指向不存在文件的记录，点开详情就 500。现在这个顺序出错顶多留一个孤儿文件，不影响数据一致性。

**分页排序必须带次级键**：`created_at` 只精确到秒，同一秒内连传两份时单靠它排序不稳定，`OFFSET` 分页会漏行或重复行。所有列表都写成 `ORDER BY created_at DESC, id DESC`。

**每个请求都查一次库**：JWT 是无状态的，签发之后无法撤销 —— 用户被禁用、账号被删，只要旧 token 没过期就还能用。`get_current_user` 每次走主键索引查一次（成本极低），换来禁用立即生效。

**CORS 收紧到白名单**：`allow_origins` 从 `["*"]` 换成 `.env` 里的 `CORS_ORIGINS`。`*` 的问题不是「谁都能调接口」—— CORS 只是浏览器策略，`curl` 和 Postman 根本不看它 —— 而是它让配置错误无处暴露：等你上线换成具体域名的那天，才会发现前端用的 origin 拼错、或者漏了 `www.`。同理 `allow_methods` 只列了实际用到的 `GET / POST / DELETE`：写 `*` 会被 Starlette 展开成 `ALL_METHODS`，等于对外声明「支持 PUT 和 PATCH」，而这两个方法一个都没实现。

`allow_credentials=False` 与「用 Bearer token」并不矛盾，这是最容易搞混的一处：CORS 里的 credentials 只涵盖 **cookie、浏览器自带的 Basic 认证、TLS 客户端证书**；代码里手动塞进 `Authorization` 头的 token 属于普通请求头，归 `allow_headers` 管。哪天改成用 cookie 存 token 才需要打开它，同时 `allow_origins` 必须全是具体域名（规范里 `*` 与 credentials 互斥）。

## 代码检查

```bash
pip install -r requirements-dev.txt   # mypy + pytest + reportlab，不进生产依赖
mypy                                  # 配置在 mypy.ini，默认检查 app/ 下的 35 个文件
pytest                                # 配置在 pytest.ini，126 个用例约 4 秒
python -m alembic check               # 库结构与模型是否一致（可选，但要连真库）
```

`mypy.ini` 里开了 `warn_unused_ignores`、`warn_redundant_casts`、`warn_unreachable`、`no_implicit_optional`、`strict_equality` —— 这几项除了 `warn_unreachable` 之外几乎零误报，属于「开了不亏」。

目前唯一的 `# type: ignore` 在 `app/core/config.py` 的 `Settings()`：`SECRET_KEY` 是必填项，但值来自运行时才读取的 `.env`，mypy 静态看不到，属于误报。

另外三个模型（`user` / `resume` / `analysis`）之间互相引用，运行时靠 relationship 的字符串类名解析，类型检查则需要 `if TYPE_CHECKING:` 下的真实导入 —— 少了它 mypy 会报 `Name "Resume" is not defined`，加上它又不会引入循环导入。

## 换行符

仓库里统一存 LF（`.gitattributes` 里 `* text=auto eol=lf`）。不加这一行的话，Windows 上 `core.autocrlf=true` 会导致两个后果：每次提交都提示 `LF will be replaced by CRLF`；以及 Linux/macOS 上 clone 后提交时出现「整个文件都被改过」的假 diff。

已经乱掉的仓库执行一次 `git add --renormalize .`。注意它**只改索引、不重写磁盘** —— 工作区文件的换行符仍然是 CRLF，想让磁盘也统一得自己按字节替换。

## 常见问题

**分析一直停在 `pending`**
worker 没启动。`pending` 只表示记录已入库、消息已投递，没有任何进程在消费队列。另开终端跑第 5 步那条 celery 命令。判断依据很直接：`started_at` 为 `null` 就说明从来没有 worker 领走过它。

**上传扫描件报「未能从文件中提取到文字」**
pdfplumber 读的是 PDF 的文字层。拍照、复印后转出来的 PDF 只有图像、没有文字层，抽出来是空字符串。这种情况需要在客户端先做 OCR，本项目不做。

**`create_all` 之后改字段没生效**
它只建不存在的表，**不改已有表结构**。改字段走 Alembic：`revision --autogenerate` → 审脚本 → `upgrade head`。

**`alembic` 一启动就报 UnicodeDecodeError**
`alembic.ini` 是按系统本地编码（中文 Windows 上是 GBK）读的，文件里不能写中文注释，否则 `'gbk' codec can't decode`。注释用英文，说明性内容放 `alembic/README`。

**改了模型，`alembic check` 说没差异 / 或者报一堆无关差异**
前者说明模型没被 import 进 `Base.metadata` —— `alembic/env.py` 里那行 `import app.models` 不能删。后者通常是 `server_default` 在作怪，本项目已把 `compare_server_default` 关掉：MySQL 会把 `CURRENT_TIMESTAMP` 回读成 `now()`，一比就永远不相等。

**连不上 MySQL / 端口对不上**
仓库里的 `docker-compose.yml` 把 MySQL 映射到 **3307**（避免和本机已装的 MySQL 抢 3306），而快速开始里那条 `docker run` 用的是 3306。`DATABASE_URL` 里的端口必须和实际映射一致。密码里含 `@` `:` `/` 时要做 URL 编码，否则会被当成连接串的分隔符。

**浏览器报 `blocked by CORS policy`**
先看控制台里那条预检（`OPTIONS`）请求的实际响应。按出现频率排：origin 少了协议头、带了结尾斜杠（`http://a.com/` 与 `http://a.com` 是两个不同字符串）、写的是 `localhost` 而浏览器访问的是 `127.0.0.1`（两者是不同 origin）、端口不对、改完 `CORS_ORIGINS` 没重启 API。

注意这类失败在服务端**不会留下任何错误**：不在名单里的来源，接口照常执行、照常返回 200，只是少一个 `Access-Control-Allow-Origin` 头，浏览器于是拒绝把响应交给页面 JS。「接口调通了但页面读不到数据」就是没匹配上。所以 CORS 不是鉴权 —— `tests/test_cors.py` 里 `test_blocked_origin_still_gets_response` 专门钉死了这一点。

## 待办

- [ ] 前端界面（上传、轮询进度、分数走势图）—— 现在只有 `/docs` 调试页
- [ ] Celery 监控（Flower）与队列积压告警
- [ ] CI：把 `mypy` / `pytest` / `alembic check` 串成一条流水线
- [ ] `latency_ms` 只反映最后一次尝试，前面超时那几次的耗时丢了
- [ ] `REQUIRED_SECTIONS` 和 `_SECTION_HINTS` 是两份手工维护的栏目列表，改一处忘另一处会出现「完整性说缺项目经历、打分却认为有」
- [ ] CI：把 `mypy` / `pytest` / `alembic check` 串成一条流水线

## 说明

个人学习项目，未做压测与安全审计，不要直接用于生产环境。
