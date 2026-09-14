"""MedReady Phase 1-3: bilingual content boundary checks.
诊前智备阶段 1-3：中英双语内容边界校验。

Required companion file / 配套文件：config/compliance_rules.json
Run the local demo / 运行演示：python3 -m core.harness_compliance

Usage / 用法：
    allowed, message = check_input(hospital + " " + service, language="both")
    if not allowed:
        # Return message and stop the runtime / 返回提示并停止主流程。
        ...
    # After JSON validation / JSON 校验后：
    safe_result = sanitize_output(validated_result, language="both")

Both languages are ALWAYS checked. language selects the response language only:
"auto" (Chinese if Chinese characters occur, otherwise English), "en", "zh",
or "both" (Chinese followed by English).
始终检查中英文规则。language 仅选择返回语言：auto、en、zh 或 both。

Rules are loaded lazily, once per process. Restart after editing the JSON, or
create a new ComplianceChecker. Missing/invalid configuration raises an error;
it never silently disables checking. No external dependencies or API calls.
规则首次使用时加载并缓存。修改后请重启或重新创建 ComplianceChecker。
配置缺失或无效时抛出异常，不会静默跳过校验。无需外部依赖或 API。

V1.0 uses strict keyword matching, including source notes. For example,
"diagnosis report" is blocked even in a logistics question. Case, punctuation,
spacing and Unicode variants are normalized; arbitrary typos, paraphrases,
negation and medical meaning are not understood. This is one safety layer,
not a guarantee that all medical advice will be detected or a legal assessment.
V1.0 严格匹配词语，包括来源说明。因此“携带诊断报告”等流程问题也会被拦截。
支持大小写、标点、空白及 Unicode 归一化，不理解任意错别字、改写、否定或医学语义。
本模块只是安全控制的一层，不保证识别所有医疗建议，也不作法律合规判断。
"""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "ComplianceChecker", "check_input", "check_output",
    "inject_disclaimer", "sanitize_output",
]

_DEFAULT_RULES = Path(__file__).resolve().parent.parent / "config" / "compliance_rules.json"
_LANGUAGES = {"auto", "en", "zh", "both"}
_CHINESE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]")


def _normalize(text: str) -> str:
    """Normalize spelling format, not meaning / 只归一化文本形式，不推断语义。"""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    # Treat straight/curly apostrophes identically / 统一直引号和弯引号。
    text = re.sub(r"['’‘`´]", "", text)
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE).strip()
    # Match Chinese phrases with intervening spacing / 合并汉字之间的空白。
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)


def _output_text(value: Any, active: set[int] | None = None, depth: int = 0) -> str:
    """Collect nested JSON string VALUES / 收集嵌套 JSON 中的字符串值。

    Includes source_note, disclaimer and unknown fields; none are trusted.
    Keys/schema validation belong to the JSON validator, not this module.
    检查来源、免责声明及其他字段的值，不把任何字段视为可信内容。
    字段名称与数据结构校验由 JSON 校验器负责。
    """
    if depth > 30:
        raise ValueError("Output nesting exceeds 30 levels / 输出嵌套超过 30 层。")
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return ""
    if not isinstance(value, (dict, list)):
        raise TypeError("Output must contain JSON-compatible values / 输出必须为 JSON 兼容数据。")
    active = set() if active is None else active
    if id(value) in active:
        raise ValueError("Circular output structure / 输出包含循环引用。")
    active.add(id(value))
    try:
        items = value.values() if isinstance(value, dict) else value
        return "\n".join(_output_text(item, active, depth + 1) for item in items)
    finally:
        active.remove(id(value))


class ComplianceChecker:
    """Load editable bilingual rules / 加载可编辑的中英文规则。"""

    def __init__(self, config_path: str | Path | None = None) -> None:
        path = Path(config_path) if config_path is not None else _DEFAULT_RULES
        rules = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rules, dict):
            raise ValueError("Rules must be a JSON object / 规则必须为 JSON 对象。")
        self._messages = rules.get("messages", {})
        if not isinstance(self._messages, dict):
            raise ValueError("Invalid messages configuration / 提示语配置无效。")
        for key in ("refusal", "disclaimer", "empty_input", "empty_output", "unavailable"):
            pair = self._messages.get(key)
            if not isinstance(pair, dict) or any(
                not isinstance(pair.get(lang), str) or not pair[lang].strip()
                for lang in ("en", "zh")
            ):
                raise ValueError(f"Missing bilingual message / 缺少双语提示：{key}")
        categories = rules.get("categories")
        if not isinstance(categories, list) or not categories:
            raise ValueError("At least one rule category is required / 至少需要一类规则。")

        self._patterns: list[re.Pattern[str]] = []
        # All complete question templates precede keywords / 完整句式优先于关键词。
        for kind in ("questions", "keywords"):
            for category in categories:
                if not isinstance(category, dict) or not isinstance(category.get(kind), dict):
                    raise ValueError("Invalid category structure / 规则类别结构无效。")
                for lang in ("en", "zh"):
                    phrases = category[kind].get(lang)
                    if not isinstance(phrases, list) or not phrases:
                        raise ValueError("Both languages require rules / 每类规则须包含中英文内容。")
                    for phrase in phrases:
                        if not isinstance(phrase, str) or not _normalize(phrase):
                            raise ValueError("Invalid rule phrase / 规则词句无效。")
                        pattern = re.escape(_normalize(phrase))
                        if lang == "en":
                            # 'cure' must not match 'secure' / 避免英文词内误匹配。
                            pattern = r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])"
                        self._patterns.append(re.compile(pattern))

    def _message(self, key: str, language: str, text: str = "") -> str:
        if language not in _LANGUAGES:
            raise ValueError("language must be auto, en, zh or both / 语言参数无效。")
        if language == "auto":
            language = "zh" if _CHINESE.search(text) else "en"
        pair = self._messages[key]
        return pair["zh"] + "\n" + pair["en"] if language == "both" else pair[language]

    def _blocked(self, text: str) -> bool:
        normalized = _normalize(text)
        return any(pattern.search(normalized) for pattern in self._patterns)

    def check_input(self, text: str, language: str = "auto") -> tuple[bool, str]:
        """Return (allowed, message) / 返回（是否允许，提示语）。

        False means stop BEFORE search or LLM calls. No user text is logged.
        返回 False 时须在搜索或调用大模型之前停止。不记录用户原始内容。
        """
        if not isinstance(text, str):
            raise TypeError("Input must be a string / 输入必须为字符串。")
        refusal = self._message("refusal", language, text)
        if not _normalize(text):
            return False, self._message("empty_input", language, text)
        return (False, refusal) if self._blocked(text) else (True, "")

    def check_output(self, result: Any, language: str = "auto") -> tuple[bool, str]:
        """Screen raw model text or JSON / 检查模型原文或 JSON 数据。

        Call before injecting trusted refusal messages. This does not validate
        the eight-field checklist schema. Unsupported or cyclic data is blocked.
        应在注入固定拒绝提示之前调用；不校验清单的八字段结构。
        无法处理或包含循环引用的数据会被拦截。
        """
        self._message("refusal", language)  # Validate language / 校验语言参数。
        try:
            text = _output_text(result)
        except (TypeError, ValueError):
            return False, self._message("empty_output", language)
        if not _normalize(text):
            return False, self._message("empty_output", language, text)
        if self._blocked(text):
            return False, self._message("refusal", language, text)
        return True, ""

    def inject_disclaimer(self, result: dict[str, Any], language: str = "auto") -> dict[str, Any]:
        """Copy the result and set one trusted disclaimer / 复制结果并写入固定免责声明。

        Replaces any existing disclaimer; never mutates the caller's dictionary.
        This method alone does NOT perform content screening; use sanitize_output.
        覆盖已有免责声明，不修改原字典。本方法单独使用不审核内容，请用 sanitize_output。
        """
        if not isinstance(result, dict):
            raise TypeError("Result must be a dictionary / 结果必须为字典。")
        text = _output_text(result)
        disclaimer = self._message("disclaimer", language, text)
        copied = deepcopy(result)
        copied["disclaimer"] = disclaimer
        return copied

    def sanitize_output(self, result: dict[str, Any], language: str = "auto") -> dict[str, Any]:
        """Check, replace if blocked, then add a disclaimer / 校验、拦截替换、追加免责。

        Input is the validated checklist dictionary (use model_dump() for a
        Pydantic object). Blocked output is replaced entirely, including sources.
        The eight checklist fields remain available; arrays remain arrays.
        compliance_blocked and message are additional UI metadata. Runtime/UI
        should show message when blocked, and never display the original output.
        输入为已校验的清单字典。被拦截时整体替换，包含来源，不保留原始输出。
        保留八个清单字段及数组类型，额外提供 compliance_blocked 和 message。
        前端应展示拒绝提示，不展示被拦截的原文。
        """
        if not isinstance(result, dict):
            raise TypeError("Result must be a dictionary / 结果必须为字典。")
        allowed, message = self.check_output(result, language)
        if not allowed:
            # Select from the original language before discarding / 丢弃前确定原文语言。
            try:
                text = _output_text(result)
            except (TypeError, ValueError):
                text = ""
            fallback = self._message("unavailable", language, text)
            safe = {
                "fasting_requirement": fallback,
                "materials_list": [],
                "appointment_process": fallback,
                "department_location": fallback,
                "estimated_cost": fallback,
                "insurance_tips": fallback,
                "attention_points": [],
                "source_note": "",
                "compliance_blocked": True,
                "message": message,
            }
        else:
            safe = deepcopy(result)
            safe["compliance_blocked"] = False
        return self.inject_disclaimer(safe, language)


@lru_cache(maxsize=1)
def _default_checker() -> ComplianceChecker:
    return ComplianceChecker()


def check_input(text: str, language: str = "auto") -> tuple[bool, str]:
    """Default input check / 使用默认规则检查输入。"""
    return _default_checker().check_input(text, language)


def check_output(result: Any, language: str = "auto") -> tuple[bool, str]:
    """Default output check / 使用默认规则检查输出。"""
    return _default_checker().check_output(result, language)


def inject_disclaimer(result: dict[str, Any], language: str = "auto") -> dict[str, Any]:
    """Inject a fixed disclaimer / 注入固定免责声明。"""
    return _default_checker().inject_disclaimer(result, language)


def sanitize_output(result: dict[str, Any], language: str = "auto") -> dict[str, Any]:
    """Screen output and return a safe replacement when blocked / 审核并替换违规输出。"""
    return _default_checker().sanitize_output(result, language)


if __name__ == "__main__":
    examples = [
        ("北京协和医院 胃镜 检查须知", True),
        ("Example Hospital MRI appointment instructions", True),
        ("胃疼吃什么药", False),
        ("Can you diagnose my symptoms?", False),
        ("Can I bring my diagnosis report?", False),
    ]
    for query, expected in examples:
        allowed, message = check_input(query, language="both")
        assert allowed is expected
        print(f"{'ALLOW / 允许' if allowed else 'BLOCK / 拦截'}: {query}")
        if message:
            print(message)

    blocked = sanitize_output({"attention_points": ["treatment plan / 治疗方案"]}, "both")
    assert blocked["compliance_blocked"]
    assert blocked["attention_points"] == []
    print("\nReplaced output / 已替换的输出：")
    print(json.dumps(blocked, ensure_ascii=False, indent=2))
    print("\nPASS / 通过：Bilingual input checks and output replacement / 双语输入检查与输出替换。")
