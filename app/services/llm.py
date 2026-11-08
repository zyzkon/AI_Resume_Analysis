"""大模型调用层。

对外只有 analyze_resume() 一个入口，mock 和真实 API 在内部切换。
上层（阶段 5 的路由、阶段 6 的 Celery 任务）不需要知道当前用的是哪个，
也不该出现 if settings.LLM_PROVIDER == ... 这种判断。

调用约定的三条：
1. 成功返回 AnalysisOutcome，里面是校验过的 AnalysisResult + 用量元数据
2. 失败一律抛 LLMError 的子类，上层只需要 except LLMError
3. 不在这一层重试网络错误超过 settings.LLM_MAX_RETRIES 次。
   任务级重试由 Celery 决定，但 apply 在任务上的策略是"不重试"：
   两层都开会让一次超时变成 3×3=9 次请求，用户等 8 分钟才看到失败
"""

import json
import logging
import re
import time
from dataclasses import dataclass

from openai import APIError, APITimeoutError, OpenAI
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.analysis import AnalysisResult, SectionCheck, Suggestion
from app.services.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

# 有些模型即使要求了 json_object，仍会礼貌地包一层 ```json ... ```
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class LLMError(Exception):
    """调用大模型失败。路由层捕获它，转成 502 或把记录标记为 failed。"""


class LLMOutputError(LLMError):
    """模型响应了，但内容不是合法 JSON 或不符合结构。可重试。"""


@dataclass(slots=True)
class AnalysisOutcome:
    """一次调用的完整结果。

    除了分析内容，还带回了 model / prompt_version / token 用量。
    这些要一起写进 analyses 表：三个月后想知道「为什么同一份简历分数变了」，
    唯一的线索就是当时用的是哪个模型、哪版提示词。
    """

    result: AnalysisResult
    model: str
    prompt_version: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


def analyze_resume(
    resume_text: str,
    job_description: str | None = None,
) -> AnalysisOutcome:
    """分析一份简历。

    Args:
        resume_text: 简历正文，来自 resumes.raw_text
        job_description: 目标岗位描述。不传则按通用技术岗标准评估

    Raises:
        LLMError: 配置错误、超时、连续重试仍拿不到可用输出
    """
    text = _prepare_text(resume_text)

    if settings.llm_is_mock:
        return _mock_analyze(text, job_description)
    return _openai_analyze(text, job_description)


def _prepare_text(text: str) -> str:
    """清洗并截断，避免把超长正文直接送进模型。

    一份 5MB 的 PDF 解析出来可能有几十万字（把页眉、水印、二维码说明都吸进来了），
    直接发过去会撞上下文上限，报错信息还很难懂。截断比报错友好。
    """
    text = (text or "").strip()
    if not text:
        raise LLMError("简历正文为空，无法分析")

    limit = settings.LLM_MAX_INPUT_CHARS
    if len(text) > limit:
        logger.warning("简历正文 %d 字，超过上限 %d，已截断", len(text), limit)
        text = text[:limit] + "\n\n[正文过长，后续内容已截断]"
    return text


# ---------------------------------------------------------------- 真实调用


def _openai_analyze(text: str, job_description: str | None) -> AnalysisOutcome:
    client = OpenAI(
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        timeout=settings.LLM_TIMEOUT,
        # 关掉 SDK 自带的自动重试，重试节奏由下面的循环控制。
        # 两层都开的话，一次超时会变成 3×3=9 次请求，用户等好几分钟还看不到错误
        max_retries=0,
    )

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(text, job_description)},
    ]

    last_error: Exception | None = None
    attempts = settings.LLM_MAX_RETRIES + 1

    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            response = client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=messages,
                temperature=settings.LLM_TEMPERATURE,
                max_tokens=settings.LLM_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
        except APITimeoutError as exc:
            last_error = exc
            logger.warning("模型调用超时（第 %d/%d 次）", attempt, attempts)
            continue
        except APIError as exc:
            # 401/402/404 这类问题重试一万次也不会好，直接抛
            raise LLMError(f"模型服务返回错误：{exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        content = (response.choices[0].message.content or "").strip()

        try:
            result = _parse_result(content)
        except LLMOutputError as exc:
            last_error = exc
            logger.warning("输出不可用（第 %d/%d 次）：%s", attempt, attempts, exc)
            if attempt == attempts:
                break
            # 把错误回灌给模型，让它知道上一版错在哪。
            # 只说「重新输出」而不指出问题，它有很大概率原样再犯一次
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": f"上面的输出不合法：{exc}\n请只返回修正后的 JSON，不要任何解释文字。",
                }
            )
            continue

        usage = response.usage
        logger.info(
            "分析完成 %dms，tokens %s+%s",
            latency_ms,
            getattr(usage, "prompt_tokens", 0),
            getattr(usage, "completion_tokens", 0),
        )
        return AnalysisOutcome(
            result=result,
            model=settings.LLM_MODEL,
            prompt_version=PROMPT_VERSION,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency_ms,
        )

    raise LLMError(f"连续 {attempts} 次未能获得可用输出：{last_error}")


def _parse_result(content: str) -> AnalysisResult:
    """把模型返回的字符串变成 AnalysisResult。

    三种常见畸形都要处理：空响应、代码块包裹、字段不合规。
    """
    if not content:
        raise LLMOutputError("模型返回了空内容")

    fenced = _FENCE.match(content)
    if fenced:
        content = fenced.group(1).strip()

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"不是合法 JSON（{exc.msg}，位置 {exc.pos}）") from exc

    if not isinstance(payload, dict):
        raise LLMOutputError(f"顶层应该是对象，实际是 {type(payload).__name__}")

    try:
        return AnalysisResult.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "顶层"
        raise LLMOutputError(f"字段 {where} 不符合要求：{first['msg']}") from exc


# ---------------------------------------------------------------- 本地 mock

_SECTION_HINTS: dict[str, tuple[str, ...]] = {
    "基本信息": ("电话", "手机", "邮箱", "@", "微信"),
    "教育背景": ("大学", "学院", "本科", "硕士", "博士", "学历", "专业"),
    "工作/实习经历": ("公司", "任职", "在职", "实习", "工作经历"),
    "项目经历": ("项目", "负责", "主导", "开发了"),
    "专业技能": ("技能", "熟悉", "掌握", "精通", "技术栈"),
    "自我评价": ("自我评价", "个人评价", "自我介绍", "性格"),
}


def _mock_analyze(text: str, job_description: str | None) -> AnalysisOutcome:
    """不联网的假实现。

    两个刻意的设计：
    1. **结果由正文内容决定，同样输入永远同样输出**。用随机数的话，
       调分页、调排序、跑回归测试时会以为代码有问题，其实只是随机到了别的值。
    2. **字段结构、字段名、取值范围和真实模型完全一致**。这样阶段 5、6
       的开发全程可以用 mock 跑，既不用等网络也不烧 token，最后换成真实
       provider 时不需要改任何下游代码。
    """
    started = time.perf_counter()

    overall = _mock_score(text)
    # 没给 JD 的时候匹配度打个折，模拟「不知道岗位，只能按通用标准估」
    match = max(20, overall - (6 if job_description is None else 2))

    present = {name: any(h in text for h in hints) for name, hints in _SECTION_HINTS.items()}

    missing = [name for name, ok in present.items() if not ok]
    completeness = [
        SectionCheck(
            name=name,
            present=ok,
            comment="已包含，信息相对完整" if ok else f"未检到相关内容，建议补充{name}",
        )
        for name, ok in present.items()
    ]

    has_metric = bool(re.search(r"\d+\s*(%|万|亿|千|ms|倍)", text))
    highlights = [
        "正文结构清晰，栏目划分能快速定位关键信息"
        if len(text) >= 400
        else "内容精简，没有冗余铺垫"
    ]
    if has_metric:
        highlights.append("包含具体数字，成果可衡量")
    if "项目" in text:
        highlights.append("有项目经历支撑，能体现实际动手能力")

    weaknesses = [f"缺少{name}相关内容" for name in missing[:2]]
    if not has_metric:
        weaknesses.append("缺少量化数据，「负责 XX」看不出实际产出")
    if "精通" in text and not has_metric:
        weaknesses.append("出现「精通」等自评词，但没有对应的事实支撑")
    if not weaknesses:
        weaknesses.append("项目描述偏技术罗列，个人贡献边界不够清楚")

    jd_note = (
        "未提供岗位描述，按通用技术岗标准估算" if job_description is None else "已结合岗位描述比对关键词"
    )

    result = AnalysisResult(
        overall_score=overall,
        summary=f"共 {len(text)} 字，检出 {len(present) - len(missing)}/{len(present)} 个标准栏目。"
        f"{'结构基本完整' if len(missing) <= 2 else '栏目缺失较多'}。",
        completeness=completeness,
        highlights=highlights,
        weaknesses=weaknesses,
        match_score=match,
        match_reason=f"{jd_note}；当前正文里能识别的技术关键词与目标岗位的对应程度为 {match}%。",
        suggestions=[
            Suggestion(
                priority="high",
                category="量化",
                issue="描述中缺少可衡量的结果",
                advice="每条经历补一个数字：耗时、并发量、用户数、成本降幅任选其一",
                example="把「优化了查询接口」改成「把订单查询 P99 从 800ms 降到 120ms」",
            ),
            Suggestion(
                priority="medium",
                category="结构",
                issue="栏目顺序或层级不够清晰",
                advice="按「基本信息 → 教育 → 工作 → 项目 → 技能」排列，招聘方扫读时最省力",
            ),
            Suggestion(
                priority="low",
                category="表达",
                issue="存在自评式表述",
                advice="把「熟悉/精通」换成具体做过的事，事实比形容词可信",
            ),
        ],
    )

    latency_ms = int((time.perf_counter() - started) * 1000)
    # 没有真的发请求，token 按字符数粗估（中文大约 1.5 字 ≈ 1 token），
    # 只为了下游统计逻辑有数可算，不代表真实计费
    return AnalysisOutcome(
        result=result,
        model="mock",
        prompt_version=PROMPT_VERSION,
        prompt_tokens=len(text) // 2,
        completion_tokens=400,
        latency_ms=latency_ms,
    )


def _mock_score(text: str) -> int:
    """让 mock 分数看起来合理，且随内容单调变化。"""
    score = 62
    if re.search(r"\d+\s*年", text):
        score += 6
    if re.search(r"\d+\s*(%|万|亿|千|ms|倍)", text):
        score += 8
    if len(text) >= 800:
        score += 5
    elif len(text) < 200:
        # 200 字以下连基本经历都说不完，一页纸的简历通常在 400-800 字
        score -= 12
    score += len(text) % 7  # 长度不同给出细微差异，避免所有简历同分
    return max(35, min(94, score))
