"""模型调用层：输出解析的三层容错、重试与错误回灌、mock 的可复现性。

为什么要单独测这一层：开发期一直用 `LLM_PROVIDER=mock`，走的是 `_mock_analyze`
**直接 return** 的分支，`_openai_analyze` 里那 80 行（重试循环、超时分支、
错误回灌、三层容错）一行都不会执行。也就是说 mock 模式下这部分是零覆盖的。

所以这里做了两件事：
1. 直接测 `_parse_result` 的各种畸形输入（纯函数，最好测）
2. 把 `OpenAI` 客户端换成假的，让 `_openai_analyze` 的循环真正跑起来

假客户端只实现一个 `chat.completions.create`，按队列依次返回「预设的响应」或
「要抛的异常」。它不关心 HTTP、不需要 key、也不需要网络。
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from openai import APIError, APITimeoutError

from app.core.config import settings
from app.services import llm
from app.services.llm import LLMError, LLMOutputError, _parse_result, _prepare_text

_SECTIONS = ("基本信息", "教育背景", "项目经历", "专业技能")


def _payload(**overrides: Any) -> dict[str, Any]:
    """一份结构完整的模型输出，测试只覆盖需要变形的字段。"""
    base: dict[str, Any] = {
        "overall_score": 70,
        "summary": "总评",
        "completeness": [{"name": name, "present": True, "comment": "已包含"} for name in _SECTIONS],
        "highlights": ["亮点"],
        "weaknesses": ["短板"],
        "match_score": 60,
        "match_reason": "依据",
        "suggestions": [
            {"priority": "high", "category": "量化", "issue": "缺少量化数据", "advice": "补数字"}
        ],
    }
    base.update(overrides)
    return base


def _dump(**overrides: Any) -> str:
    return json.dumps(_payload(**overrides), ensure_ascii=False)


# ---------------------------------------------------------------- _parse_result


def test_parse_result_accepts_plain_json() -> None:
    result = _parse_result(_dump())

    assert result.overall_score == 70
    assert len(result.completeness) == 4


def test_parse_result_strips_json_fence() -> None:
    """有些模型即使要求了 json_object，仍然会礼貌地包一层 ```json。

    识别围栏比让它重试便宜得多 —— 重试要再花几秒和一份 token。
    """
    fenced = f"```json\n{_dump()}\n```"

    assert _parse_result(fenced).overall_score == 70


def test_parse_result_rejects_empty_content() -> None:
    with pytest.raises(LLMOutputError, match="空内容"):
        _parse_result("")


def test_parse_result_rejects_non_json() -> None:
    with pytest.raises(LLMOutputError, match="不是合法 JSON"):
        _parse_result("抱歉，我无法分析这份简历。")


def test_parse_result_rejects_non_object_top_level() -> None:
    with pytest.raises(LLMOutputError, match="顶层应该是对象"):
        _parse_result(json.dumps([1, 2, 3]))


def test_parse_result_clamps_out_of_range_scores() -> None:
    """105 分和 -3 分钳到区间内，而不是报错重试。

    重试要再花一次调用，而钳完之后的信息损失可以忽略。
    """
    result = _parse_result(_dump(overall_score=105, match_score=-3))

    assert result.overall_score == 100
    assert result.match_score == 0


def test_parse_result_rounds_float_scores() -> None:
    assert _parse_result(_dump(overall_score=72.6)).overall_score == 73


def test_parse_result_normalizes_priority_and_category() -> None:
    """模型写 urgent / 高 / 排版 都收下。

    这个字段只影响排序，认错了顶多顺序差一点；但如果因为一个词不合法
    就让整条校验失败，代价是一次重试。
    """
    result = _parse_result(
        _dump(
            suggestions=[
                {
                    "priority": "urgent",
                    "category": "排版",
                    "issue": "a",
                    "advice": "b",
                },
                {
                    "priority": "完全没听过",
                    "category": "完全是编的",
                    "issue": "c",
                    "advice": "d",
                },
            ]
        )
    )

    assert result.suggestions[0].priority == "high"
    assert result.suggestions[0].category == "结构"
    # 认不出来的一律落到中间档 / 默认分类，不能报错
    assert result.suggestions[1].priority == "medium"
    assert result.suggestions[1].category == "内容"


def test_parse_result_truncates_excess_suggestions() -> None:
    """给了 10 条只留 8 条，防止响应体膨胀。"""
    suggestions = [
        {"priority": "low", "category": "表达", "issue": f"问题{index}", "advice": "改"}
        for index in range(10)
    ]

    assert len(_parse_result(_dump(suggestions=suggestions)).suggestions) == 8


def test_parse_result_requires_at_least_four_sections() -> None:
    """只检查一两个栏目说明模型没认真做，宁可重试。"""
    with pytest.raises(LLMOutputError, match="至少需要检查 4 个栏目"):
        _parse_result(_dump(completeness=[{"name": "基本信息", "present": True, "comment": "ok"}]))


def test_parse_result_rejects_empty_highlights() -> None:
    with pytest.raises(LLMOutputError, match="highlights"):
        _parse_result(_dump(highlights=[]))


# ---------------------------------------------------------------- _prepare_text


def test_prepare_text_rejects_blank() -> None:
    with pytest.raises(LLMError, match="正文为空"):
        _prepare_text("   \n  ")


def test_prepare_text_truncates_long_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """截断比报错友好：超长正文撞的是上下文上限，报错信息通常很难懂。"""
    monkeypatch.setattr(settings, "LLM_MAX_INPUT_CHARS", 10)

    text = _prepare_text("x" * 20)

    assert text.startswith("x" * 10)
    assert "已截断" in text


# ---------------------------------------------------------------- _openai_analyze（假客户端）


class _FakeCompletions:
    """按队列吐响应或抛异常的假 completions 端点。"""

    def __init__(self, queue: list[Any]) -> None:
        self._queue = list(queue)
        self.calls: list[list[dict[str, Any]]] = []

    def create(self, **kwargs: Any) -> Any:
        # 存一份快照：messages 是同一个 list，重试时会原地 append，
        # 直接存引用的话所有记录都会变成最终状态
        self.calls.append(list(kwargs["messages"]))
        item = self._queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _response(content: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=22),
    )


def _install(monkeypatch: pytest.MonkeyPatch, queue: list[Any]) -> _FakeCompletions:
    completions = _FakeCompletions(queue)
    monkeypatch.setattr(
        llm,
        "OpenAI",
        lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=completions)),
    )
    return completions


def test_openai_analyze_returns_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    completions = _install(monkeypatch, [_response(_dump())])

    outcome = llm._openai_analyze("一份简历", None)

    assert outcome.result.overall_score == 70
    assert outcome.model == settings.LLM_MODEL
    assert outcome.prompt_tokens == 11
    assert outcome.completion_tokens == 22
    assert outcome.latency_ms >= 0
    assert len(completions.calls) == 1


def test_openai_analyze_retries_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时值得重试：可能只是这一次卡住了。

    attempts = LLM_MAX_RETRIES + 1，这里把它压到 2 次，避免用例默认跑 3 轮。
    """
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 1)
    completions = _install(
        monkeypatch,
        [APITimeoutError(request=None), _response(_dump())],
    )

    outcome = llm._openai_analyze("一份简历", None)

    assert outcome.result.overall_score == 70
    assert len(completions.calls) == 2


def test_openai_analyze_feeds_error_back_to_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """第二轮的 messages 里必须带上「上一版坏输出 + 具体哪里不合法」。

    只说「重新输出」而不指出问题，模型有很大概率原样再犯一次。
    """
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 1)
    completions = _install(monkeypatch, [_response("这不是 JSON"), _response(_dump())])

    llm._openai_analyze("一份简历", None)

    first, second = completions.calls
    assert len(first) == 2  # system + user
    assert len(second) == 4  # 追加了 assistant 坏输出 + user 纠错
    assert second[-2] == {"role": "assistant", "content": "这不是 JSON"}
    assert second[-1]["content"].startswith("上面的输出不合法")
    assert "不是合法 JSON" in second[-1]["content"]


def test_openai_analyze_gives_up_after_all_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_MAX_RETRIES", 2)
    completions = _install(
        monkeypatch,
        [_response("坏输出"), _response("还是坏"), _response("依然坏")],
    )

    with pytest.raises(LLMError, match="连续 3 次未能获得可用输出"):
        llm._openai_analyze("一份简历", None)

    assert len(completions.calls) == 3


def test_openai_analyze_does_not_retry_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """401/402/404 这类问题重试一万次也不会好，必须立刻抛。

    这条同时钉死了「两层重试不能叠加」：SDK 的 max_retries 已经设成 0，
    任务层也是 0，所以一次鉴权失败只会有一次请求。
    """
    completions = _install(monkeypatch, [APIError("鉴权失败", None, body=None)])

    with pytest.raises(LLMError, match="模型服务返回错误"):
        llm._openai_analyze("一份简历", None)

    assert len(completions.calls) == 1


# ---------------------------------------------------------------- mock


def test_mock_is_deterministic() -> None:
    """同样输入必须永远同样输出。

    用随机数的话，调分页、调排序、跑回归测试时会以为代码有问题，
    其实只是随机到了别的值。
    """
    text = "张三 本科 熟悉 Python，负责订单系统开发，QPS 提升 3 倍"

    first = llm._mock_analyze(text, None).result.model_dump()
    second = llm._mock_analyze(text, None).result.model_dump()

    assert first == second


def test_mock_score_rewards_metrics_and_length() -> None:
    """分数随内容单调变化，不是常量。

    带年限 + 量化指标 + 足够长的正文，一定高于一份几百字的空泛简历。
    """
    rich = llm._mock_analyze("工作 3 年，QPS 提升 3 倍，" + "项目经历。" * 200, None).result
    plain = llm._mock_analyze("简历正文", None).result

    assert rich.overall_score > plain.overall_score
    assert "包含具体数字，成果可衡量" in rich.highlights


def test_mock_jd_lowers_match_score_when_absent() -> None:
    """没给 JD 时匹配度打折，模拟「不知道岗位，只能按通用标准估」。"""
    text = "张三 本科 熟悉 Python"

    without_jd = llm._mock_analyze(text, None).result
    with_jd = llm._mock_analyze(text, "招聘 Python 后端").result

    assert without_jd.match_score < with_jd.match_score
    assert "未提供岗位描述" in without_jd.match_reason


def test_mock_checks_all_standard_sections() -> None:
    result = llm._mock_analyze("某某大学 本科，负责一个项目，熟悉 Python", None).result

    present = {item.name: item.present for item in result.completeness}
    assert len(present) == 6
    assert present["教育背景"] is True
    assert present["项目经历"] is True
    # 正文里没有任何联系方式，「基本信息」应当判定为缺失
    assert present["基本信息"] is False


def test_analyze_resume_dispatches_to_mock() -> None:
    """测试环境固定 LLM_PROVIDER=mock，入口函数要走到假实现上。"""
    outcome = llm.analyze_resume("张三 本科 熟悉 Python")

    assert outcome.model == "mock"
    assert outcome.result.overall_score > 0
