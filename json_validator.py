"""Phase 2-4: preparation result validation / 阶段 2-4：就诊准备结果校验。

Requires Pydantic v2 (project pin: pydantic==2.13.5).
依赖 Pydantic v2（项目版本：2.13.5）。

Pipeline / 调用顺序:
    raw = llm.chat(system_prompt, user_prompt, json_mode=True)
    result = validate_result(json.loads(raw))
    data = result.model_dump()
    # Next apply output compliance checks / 然后进行输出合规检查。

All eight fields are present in the result. Missing/null/blank text gets the
bilingual fallback. Missing/null lists get [DEFAULT_TEXT]; explicit empty lists
remain empty. List items must be nonblank strings. Unknown fields and incorrect
types are rejected, not silently discarded or converted into facts.
结果始终包含八个字段。缺失/null/空白文本使用双语默认提示；缺失/null 列表使用
[DEFAULT_TEXT]，明确传入的空列表保留。列表项必须为非空字符串。
未知字段及错误类型会被拒绝，不会静默丢弃或转换为事实。

This validates structure only: it does not verify hospital facts, URLs, or medical
compliance. Do not pass the compliance-enriched object back through this model;
its disclaimer and blocked flags belong to the later output layer.
仅校验结构，不核实医院事实、网址或医疗合规。合规处理后的对象包含额外标记及声明，
属于后续输出层，不应重新传入此八字段模型。

Run from the project root / 在项目根目录运行:
    python3 -m utils.json_validator --demo
    python3 -m utils.json_validator --interactive
"""
from __future__ import annotations

import argparse
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

__all__ = ["PreparationResult", "validate_result", "DEFAULT_TEXT"]

DEFAULT_TEXT = (
    "暂无明确说明，以医院现场为准。\n"
    "Information unavailable. Please confirm requirements with the hospital."
)
_TEXT_FIELDS = (
    "fasting_requirement", "appointment_process", "department_location",
    "estimated_cost", "insurance_tips", "source_note",
)
_LIST_FIELDS = ("materials_list", "attention_points")
NonblankText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class PreparationResult(BaseModel):
    """Eight-field preparation checklist / 八字段就诊准备清单。

    Fields can be omitted on input because defaults are supplied; all are included
    by model_dump() on output. Avoid exclude_unset=True when sending the checklist.
    输入可省略字段，默认值自动补齐；model_dump() 输出包含全部字段。
    输出清单时不要使用 exclude_unset=True。
    """
    model_config = ConfigDict(
        strict=True, extra="forbid", validate_default=True,
        validate_assignment=True, hide_input_in_errors=True,
    )

    fasting_requirement: NonblankText = Field(default=DEFAULT_TEXT, description="Fasting requirements / 空腹要求")
    materials_list: list[NonblankText] = Field(default_factory=lambda: [DEFAULT_TEXT], description="Required materials / 携带材料")
    appointment_process: NonblankText = Field(default=DEFAULT_TEXT, description="Appointment process / 预约流程")
    department_location: NonblankText = Field(default=DEFAULT_TEXT, description="Department location / 科室位置")
    estimated_cost: NonblankText = Field(default=DEFAULT_TEXT, description="Estimated cost / 预计费用")
    insurance_tips: NonblankText = Field(default=DEFAULT_TEXT, description="Insurance information / 医保提示")
    attention_points: list[NonblankText] = Field(default_factory=lambda: [DEFAULT_TEXT], description="Points to note / 注意事项")
    source_note: NonblankText = Field(default=DEFAULT_TEXT, description="Source information / 来源说明")

    @field_validator(*_TEXT_FIELDS, mode="before")
    @classmethod
    def fill_empty_text(cls, value):
        """Treat null/blank as unknown, not a factual answer / null 或空白代表信息未知。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            return DEFAULT_TEXT
        return value

    @field_validator(*_LIST_FIELDS, mode="before")
    @classmethod
    def fill_null_list(cls, value):
        return [DEFAULT_TEXT] if value is None else value


def validate_result(data: dict) -> PreparationResult:
    """Validate a parsed JSON object without changing input / 校验已解析 JSON 字典，不修改原输入。

    Raises TypeError for non-dict input and Pydantic ValidationError for bad fields.
    非字典输入抛出 TypeError；字段无效抛出 Pydantic ValidationError。
    """
    if not isinstance(data, dict):
        raise TypeError("data must be a dictionary; parse JSON text first / data 必须为字典，请先解析 JSON 字符串。")
    return PreparationResult.model_validate(data)


def _demo() -> int:
    """Offline tests: no network, key or LLM required / 离线测试，无需网络、密钥或大模型。"""
    from copy import deepcopy
    sample = {"materials_list": [" Example document / 示例材料 "], "source_note": "Demo only / 仅为演示"}
    original = deepcopy(sample)
    result = validate_result(sample)
    assert sample == original
    assert set(result.model_dump()) == set(_TEXT_FIELDS + _LIST_FIELDS)
    assert result.materials_list == ["Example document / 示例材料"]
    assert result.fasting_requirement == DEFAULT_TEXT
    assert validate_result({"source_note": "  ", "materials_list": None}).materials_list == [DEFAULT_TEXT]
    assert validate_result({"materials_list": []}).materials_list == []
    assert validate_result({"estimated_cost": None}).estimated_cost == DEFAULT_TEXT
    first, second = validate_result({}), validate_result({})
    first.materials_list.append("Demo")
    assert second.materials_list == [DEFAULT_TEXT]
    for invalid in ({"materials_list": "ID"}, {"estimated_cost": 100},
                    {"attention_points": [None]}, {"attention_points": [" "]},
                    {"unexpected": "value"}, {"materials_list": ("ID",)}):
        try:
            validate_result(invalid)
        except ValidationError:
            pass
        else:
            raise AssertionError("Invalid fields accepted / 错误字段被接受。")
    try:
        validate_result('"not a dictionary"')
    except TypeError:
        pass
    else:
        raise AssertionError("Non-dictionary accepted")
    assert validate_result(json.loads(result.model_dump_json())) == result
    print(result.model_dump_json(indent=2))
    print("PASS / 通过：defaults, types, empty values, independent lists and JSON round trip / 默认值、类型、空值、独立列表及 JSON 转换。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Preparation JSON validator / 就诊准备 JSON 校验器")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo", action="store_true", help="Offline tests / 离线测试")
    modes.add_argument("--interactive", action="store_true", help="Paste one line of JSON / 输入一行 JSON")
    args = parser.parse_args()
    if args.demo:
        return _demo()
    if not args.interactive:
        parser.print_help()
        return 0
    try:
        raw = input('JSON object, e.g. {} / 输入一行 JSON 对象，例如 {}: ')
        result = validate_result(json.loads(raw))
        print(result.model_dump_json(indent=2))
        return 0
    except json.JSONDecodeError:
        print("Invalid JSON syntax / JSON 语法错误。")
    except ValidationError as exc:
        # Do not print raw user values or arbitrary unknown field names.
        # 不打印原始用户内容或未知字段名称。
        print("Invalid field types or unknown fields / 字段类型错误或存在未知字段。")
        for error in exc.errors(include_input=False, include_url=False):
            field = error["loc"][0] if error["loc"] else ""
            label = field if field in PreparationResult.model_fields else "unknown field / 未知字段"
            print(f"  {label}: {error['type']}")
    except TypeError as exc:
        print(str(exc))
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled / 已取消。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
