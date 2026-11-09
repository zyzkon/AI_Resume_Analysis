# AI 简历分析

上传一份 PDF / DOCX 简历，调用大模型输出结构化评分：简历完整度、内容质量、岗位匹配度，以及按优先级排序的修改建议。

FastAPI + SQLAlchemy + MySQL + Redis + Celery，同步接口一律在线程里跑完，耗时几十秒的模型调用交给独立 worker 异步执行。

> 个人学习项目，接口契约、Prompt 和表结构都还在迭代，不保证向后兼容。

## 功能

- 邮箱注册 / 登录，JWT 鉴权（bcrypt 存密码）
- 上传 PDF / DOCX，服务端解析出纯文本入库（扩展名 + 魔数双校验，5 MB 上限）
- 提交分析任务后**立即返回 202**，worker 后台调用模型，前端轮询状态
- 分析结果结构化存储：总分、各栏目检查、建议列表、模型名、Prompt 版本
- 支持传入目标岗位 JD，输出岗位匹配度
- **历史对比**：同一份简历多次分析的分数走势，以及两次分析之间的建议变化（新增 / 已解决 / 仍存在）
- 全部资源按用户隔离，越权访问统一返回 404 而不是 403，不泄露资源是否存在

## 技术栈

| 层 | 选型 |
|---|---|
| Web | FastAPI 0.162 · Uvicorn |
| 数据 | SQLAlchemy 2.1 · MySQL 8（utf8mb4）· PyMySQL |
| 队列 | Celery 5.6 · Redis 7+ |
| 校验 | Pydantic 2 · pydantic-settings |
| 模型 | OpenAI 兼容接口（默认 DeepSeek），内置 `mock` 模式可离线跑通全流程 |
| 解析 | pdfplumber · python-docx |

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

库要先建好，表由脚本创建。登录 MySQL（`mysql -uroot -p`）后执行：

```sql
CREATE DATABASE resume_db DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'resume'@'%' IDENTIFIED BY 'your-password';
GRANT ALL PRIVILEGES ON resume_db.* TO 'resume'@'%';
```

```bash
python -m app.db.init_db
```

`create_all` 只建不存在的表，**不会改已有表结构**。改字段要手动 ALTER 或上 Alembic。

### 5. 启动（两个终端）

```bash
# 终端 1：API
uvicorn app.main:app --reload

# 终端 2：worker
celery -A app.workers.celery_app:celery_app worker -l info -P solo
```

Windows 必须加 `-P solo`（或 `-P threads`），Celery 的 prefork 模型在 Windows 上不可用。

**只起 API 不起 worker，分析记录会永远停在 `pending`** —— 这是异步系统的正常表现，不是 bug。

### 6. 试一试

- Swagger：<http://127.0.0.1:8000/docs>，点右上角 Authorize 填 `access_token`
- 命令行：用 `test_main.http`（PyCharm / IntelliJ HTTP Client），按顺序点绿色箭头即可，`token` / `resume_id` / `analysis_id` 会自动串起来

验证链路是否通畅：

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","version":"0.1.0","debug":true}
```

如果本机开了全局代理，curl 访问 127.0.0.1 可能被代理拦成 502，加 `--noproxy '*'`。

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
| 502 | 模型服务返回错误（上游问题） |
| 503 | 任务队列不可用（Redis 挂了），可退避重试 |

## 目录结构

```
app/
├── main.py               FastAPI 实例、中间件、路由注册
├── api/
│   ├── deps.py           get_current_user：JWT -> User
│   ├── openapi.py        tag 说明与标准错误响应（文档素材，不含逻辑）
│   └── routes/           auth / resumes / analyses 三组端点
├── core/
│   ├── config.py         Settings，环境变量的唯一来源
│   └── security.py       密码哈希、JWT 签发与校验
├── db/
│   ├── session.py        engine、连接池、SessionLocal
│   └── init_db.py        建表脚本
├── models/               SQLAlchemy 表定义
├── schemas/              Pydantic 入参 / 出参
│   ├── system.py         健康检查出参
│   └── comparison.py     对比与走势的出参模型
├── services/             业务逻辑，不认识 FastAPI 也不认识 Celery
│   ├── parser.py         PDF / DOCX -> 纯文本
│   ├── prompts.py        Prompt 与版本号
│   ├── llm.py            调模型、解析与校验输出、重试
│   ├── analysis.py       一次分析的完整编排，可同步调用
│   └── comparison.py     两次分析的差异计算，纯函数
└── workers/
    ├── celery_app.py     Celery 实例与配置
    └── tasks.py          任务薄壳：Session 生命周期 + 超时信号
```

分层的意义：`services/` 里的东西既能被路由调用，也能被 worker 调用，还能被脚本和单元测试直接调用 —— 想批量重跑历史分析，`for` 循环调 `execute_analysis(db, id)` 就行，不用起 Redis。

## 关键设计

**异步而不丢任务**：提交时先落一条 `pending` 记录再 `.delay()`。顺序反过来的话，worker 可能在记录还不存在时就开始跑。任务开了 `acks_late`，worker 中途被杀消息会重新投递，所以 `execute_analysis` 里做了幂等判断（已是 `done` 就跳过）。

**两层超时**：Celery 的 soft limit 300 秒先抛 `SoftTimeLimitExceeded`，任务捕获后把记录标成 `failed` 再退出；hard limit 330 秒由 worker 直接杀进程，兜住「异常处理里又卡住」的情况。这两个值必须大于 `LLM_TIMEOUT × (LLM_MAX_RETRIES + 1)`，否则会误杀还在正常重试的请求。

**连接池**：`pool_pre_ping` + `pool_recycle=3600` 应付 MySQL 的 `wait_timeout`；prefork 模式下子进程会在 `worker_process_init` 里 `engine.dispose(close=False)`，否则多个进程共用同一个 TCP 连接，会出现只在高并发下才犯的「Packet sequence number wrong」。

**上传六步**：扩展名 → 累加大小 → 魔数 → 落盘 → 解析 → 写库。后三步任一失败都要删掉已落盘的文件，否则磁盘会被垃圾文件慢慢堆满。

**输出容错分层**：数值越界钳制（105 → 100）、枚举拼错归一化（`urgent` → `high`）、列表过长截断，只有「不是 JSON」「缺必填字段」才触发重试 —— 目的是把重试次数压到最低，每次重试都在烧 token。

**建议的模糊匹配**：对比两次分析时，判断「这条建议上次也有」用的是 `difflib.SequenceMatcher` 的字符级相似度，阈值 0.55。去掉标点和空白后比较，因为模型两次输出的标点几乎不可能一致。0.55 这个位置：同一个问题改了措辞通常落在 0.6~0.8，话题相近但内容不同的两个问题通常在 0.4 以下。选字符级而不是分词，是因为中文没有空格，而引入分词库只为做一次比较并不划算。

**对比不落库**：`trend` 和 `compare` 完全由已有的 `result` JSON 现算，不新增表、不缓存。代价是每次请求都要把 JSON 反序列化再校验一遍，收益是改算法时历史数据自动跟上 —— 如果把 diff 结果存表，算法一升级，旧记录就永远是新旧混杂的。目前一份简历几十次分析的规模，现算的开销可以忽略。

**为什么 409 而不是 400**：拿 `pending` 的分析做对比，请求本身没写错，是资源当前状态不允许这个操作。前端拿到 409 应该提示「等分析跑完再看」，拿到 400 则会以为参数填错了。

**错误码进文档**：FastAPI 默认只把成功码和 422 写进 OpenAPI，客户端想知道「什么时候返回 409」只能翻源码。`api/openapi.py` 把每个错误码的描述和响应示例集中定义成常量，路由里 `responses={**AUTH_REQUIRED, 404: NOT_FOUND_RESUME}` 引用 —— 14 个端点共用一份措辞，不会出现「这个接口写『无效凭证』、那个写『认证失败』」的漂移。422 刻意不覆盖，框架自动生成的那份带着字段级的 `loc` / `msg` / `type`，比手写的示例信息量大。

**401 文案统一在中文**：`OAuth2PasswordBearer` 默认会在「没带 Authorization 头」时自己抛 401，文案是写死的英文 `Not authenticated`。传 `auto_error=False` 把这一步收回 `get_current_user` 里，全站 401 只有三种文案（未提供身份凭证 / 无效的凭证 / 登录已过期），前端不用为一条英文单独写判断。

## 代码检查

```bash
pip install -r requirements-dev.txt   # 只有 mypy，不污染生产依赖
mypy                                  # 配置在 mypy.ini，默认检查 app/ 下的 30 个文件
```

`mypy.ini` 里开了 `warn_unused_ignores`、`warn_redundant_casts`、`warn_unreachable`、`no_implicit_optional`、`strict_equality` —— 这几项除了 `warn_unreachable` 之外几乎零误报，属于「开了不亏」。

目前唯一的 `# type: ignore` 在 `app/core/config.py` 的 `Settings()`：`SECRET_KEY` 是必填项，但值来自运行时才读取的 `.env`，mypy 静态看不到，属于误报。

另外三个模型（`user` / `resume` / `analysis`）之间互相引用，运行时靠 relationship 的字符串类名解析，类型检查则需要 `if TYPE_CHECKING:` 下的真实导入 —— 少了它 mypy 会报 `Name "Resume" is not defined`，加上它又不会引入循环导入。

## 换行符

仓库里统一存 LF（`.gitattributes` 里 `* text=auto eol=lf`）。不加这一行的话，Windows 上 `core.autocrlf=true` 会导致两个后果：每次提交都提示 `LF will be replaced by CRLF`；以及 Linux/macOS 上 clone 后提交时出现「整个文件都被改过」的假 diff。

## 待办

- [ ] Alembic 迁移（现在 `create_all` 只能建表，改字段要手工 ALTER）
- [ ] pytest 测试套件（目前只有 `test_main.http` 手工验证）
- [ ] 分析结果历史对比（同一份简历的不同版本、不同 Prompt 版本）
- [ ] 生产环境收紧 CORS（当前 `allow_origins=["*"]`）
- [ ] Celery 监控（Flower）与队列积压告警
- [ ] 僵尸任务清理（`pending` / `running` 超时的记录）

## 说明

个人学习项目，未做压测与安全审计，不要直接用于生产环境。
