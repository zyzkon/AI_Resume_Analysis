"""OpenAPI 文档的公共素材：tag 说明 + 标准错误响应。

为什么单独抽一个文件，而不是在每个装饰器里直接写字面量？

1. **一致性**。14 个端点里，401 的含义完全一样（没带 token / token 无效 / 过期），
   404 的含义也基本一样（不存在，或者存在但不属于你）。分散写 14 遍，
   必然出现「这个接口写"无效凭证"、那个写"认证失败"」的漂移，
   而且改错别字要改 14 处。
2. **默认行为不够用**。FastAPI 只会自动列出成功码和 422 校验错误，
   其余状态码在 /docs 里根本看不到。客户端想知道「这个接口什么时候返回 409」，
   只能去翻源码或者抓包试。把错误码写进文档，等于把异常契约公开出来。

用法：

    @router.get("/{id}", responses={**AUTH_REQUIRED, 404: NOT_FOUND_RESUME})
    def endpoint(...): ...

注意 `responses` 的键写 int 而不是字符串，FastAPI 会自己转成 OpenAPI 要求的字符串键。

这里刻意不覆盖 422：FastAPI 自动生成的那份带着完整的字段级错误结构
（loc / msg / type），比手写的 example 信息量大得多。
"""

from typing import Any


def _error(description: str, detail: str) -> dict[str, Any]:
    """构造一条标准错误响应定义。

    `detail` 只是示例值，用于让文档读者一眼看出响应体长什么样。
    真实响应里的 detail 可能是动态的（比如带上了具体的 id）。
    """
    return {
        "description": description,
        "content": {"application/json": {"example": {"detail": detail}}},
    }


# ---------------------------------------------------------------- 单个错误

UNAUTHORIZED = _error(
    "认证失败。`detail` 有三种文案，都是 401：\n\n"
    "- `未提供身份凭证` —— 请求头里根本没有 `Authorization`\n"
    "- `无效的凭证` —— 签名不对、不是合法 JWT、`sub` 缺失或不是数字（刻意不区分，避免泄露试探进度）\n"
    "- `登录已过期，请重新登录` —— 签名有效但 `exp` 已过\n\n"
    "响应头带 `WWW-Authenticate: Bearer`，这是 RFC 6750 的硬性要求，"
    "缺了它多数 HTTP 客户端不会把它识别为认证失败。",
    "未提供身份凭证",
)

INVALID_CREDENTIALS = _error(
    "邮箱或密码错误。**两种情况返回完全相同的状态码和文案** —— "
    "一旦区分开（比如「该邮箱未注册」），攻击者拿一个邮箱试一次就能判断它是否注册过。"
    "响应耗时也被刻意对齐过：用户不存在时同样会跑一次 bcrypt 校验",
    "邮箱或密码错误",
)

FORBIDDEN = _error(
    "身份有效但账号已被禁用。与 401 的区别：401 是「不知道你是谁」，403 是「知道你是谁但不让你进」",
    "账号已被禁用",
)

NOT_FOUND_RESUME = _error(
    "简历不存在，或者存在但属于其他用户 —— 两种情况统一按「不存在」处理，"
    "避免通过状态码差异枚举出别人的资源 id",
    "简历不存在",
)

NOT_FOUND_ANALYSIS = _error(
    "分析记录不存在，或者不属于当前用户",
    "分析记录不存在",
)

BAD_REQUEST_EMPTY_TEXT = _error(
    "这份简历解析出来的正文是空的（例如扫描件），没有内容可供分析。"
    "这种请求不会占用队列名额，在创建记录之前就被拦下",
    "这份简历没有可用正文，无法分析",
)

BAD_REQUEST_UPLOAD = _error(
    "三种情况：文件是空的、解析失败（损坏或加了密码的 PDF/DOCX）、"
    "解析出的正文超过 60000 字节（MySQL TEXT 列的余量上限）。"
    "三种情况都会**先把已落盘的文件删掉**再返回，不留孤儿文件",
    "无法解析这个文件，可能是扫描件或已损坏",
)

SAVE_FAILED = _error(
    "文件已落盘但写数据库失败（连接中断、约束冲突等）。"
    "此时磁盘文件会被删掉，不会留下没有数据库记录的孤儿",
    "保存简历失败",
)

BAD_REQUEST_SAME_ID = _error(
    "拿同一条分析做对比（`base_id == target_id`）。请求本身写错了，"
    "所以是 400；而「分析还没跑完」那种情况是资源状态不允许，走 409",
    "对比的两个分析 id 不能相同",
)

CONFLICT_EMAIL = _error(
    "邮箱已被注册。靠数据库唯一索引判定，不是先查后插 —— 后者存在并发窗口",
    "该邮箱已被注册",
)

CONFLICT_UNFINISHED = _error(
    "参与对比的分析还没跑到 `done`（处于 pending / running），或者已经 failed。"
    "请求本身没写错，是资源当前状态不允许这个操作，所以是 409 而不是 400",
    "分析记录 5 尚未完成（status=running），无法对比",
)

TOO_LARGE = _error(
    "文件超过 `MAX_UPLOAD_MB` 上限。边读边累加，超限立刻中断，不会把整个文件读进内存",
    "文件超过 5MB 上限",
)

UNSUPPORTED_MEDIA = _error(
    "扩展名不在 .pdf / .docx 之内，或者文件头魔数与扩展名不符（例如把 .exe 改名成 .pdf）",
    "只支持 PDF 和 DOCX，收到的是 .txt",
)

QUEUE_UNAVAILABLE = _error(
    "任务没能投进队列（Redis / broker 不可用）。此时记录已被标为 `failed` 并写入了原因，"
    "不会留下一条永远 pending 的僵尸记录。503 而不是 500：依赖暂时故障，退避后重试有意义",
    "分析队列暂不可用，请稍后重试",
)

INTERNAL = _error(
    "服务端内部错误。分析相关接口在遇到历史脏数据（result JSON 结构失效）时也走这里，"
    "detail 里会带上出问题的记录 id",
    "服务器内部错误",
)


# ---------------------------------------------------------------- 组合

# 键的类型写成 int | str 是为了对齐 FastAPI 的 responses 参数签名 ——
# 它两种都收（"404" 和 404 等价）。标成纯 int 的话，
# 往 responses= 里传或者用 ** 展开时 mypy 会报 keys 类型不兼容。

# 所有需要登录的端点共用
AUTH_REQUIRED: dict[int | str, dict[str, Any]] = {401: UNAUTHORIZED, 403: FORBIDDEN}

# 需要登录 + 需要定位一份自己的简历
OWNED_RESUME: dict[int | str, dict[str, Any]] = {**AUTH_REQUIRED, 404: NOT_FOUND_RESUME}


# ---------------------------------------------------------------- tag 分组

tags_metadata: list[dict[str, Any]] = [
    {
        "name": "系统",
        "description": "健康检查。**不需要认证**，用于容器探活和部署后验收。",
    },
    {
        "name": "用户",
        "description": (
            "注册、登录、获取当前用户。\n\n"
            "登录有两个入口，返回的 token **完全等价**，按调用方习惯二选一：\n\n"
            "| 端点 | 请求格式 | 谁在用 |\n"
            "| --- | --- | --- |\n"
            "| `POST /auth/token` | `application/x-www-form-urlencoded` | Swagger 的 Authorize 按钮 |\n"
            "| `POST /auth/login` | `application/json` | 前端 |\n\n"
            "**`/auth/token` 的 `username` 字段要填邮箱**，这是 OAuth2 规范规定的字段名，改不了。\n\n"
            "拿到 token 后点右上角 **Authorize** 填入即可调试受保护接口，"
            "不需要手动加 `Authorization` 请求头。"
        ),
    },
    {
        "name": "简历",
        "description": (
            "上传、列表、详情、删除。全部需要认证，且只能操作**自己名下**的简历。\n\n"
            "上传链路会依次校验：扩展名 → 文件大小 → 文件头魔数 → 解析 → 写库。"
            "任何一步失败都会把已落盘的文件删掉，不会留下没有数据库记录的孤儿文件。\n\n"
            "越权访问统一返回 **404 而不是 403** —— 403 等于承认「这个 id 确实存在，只是你没权限」，"
            "反而给了攻击者枚举 id 的信号。"
        ),
    },
    {
        "name": "分析",
        "description": (
            "提交 AI 分析任务、轮询结果、查看历史、对比与走势。\n\n"
            "**提交是异步的**：接口建一条 `pending` 记录后立刻返回 **202**，"
            "模型调用在 Celery worker 里跑：\n\n"
            "```\n"
            "202 pending  →  worker 领走  →  running  →  done / failed\n"
            "```\n\n"
            "所以调用方必须拿返回的 `id` 轮询 `GET /analyses/{id}` 直到状态变成 `done` 或 `failed`。\n"
            "`created_at` 与 `started_at` 之差就是排队耗时；`started_at` 为 null 说明从来没有 worker 领走过它。"
        ),
    },
]
