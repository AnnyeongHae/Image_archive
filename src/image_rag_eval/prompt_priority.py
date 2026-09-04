"""Heuristic structural prompt ranking only; not a semantic quality judgment."""
from __future__ import annotations

import json
import re
from typing import Any


TIER_LABELS = {
    1: "tier1_useful_structured_json",
    2: "tier2_structured_controls",
    3: "tier3_descriptive_natural_language",
    4: "tier4_minimal_or_empty",
}

_ARGUMENT_RE = re.compile(r"\{argument\b[^{}]*\}", re.IGNORECASE)
_MUSTACHE_RE = re.compile(r"\{\{[^{}]+\}\}")
_SECTION_LINE_RE = re.compile(r"(?m)^\s*[\w\u00C0-\u024F\u4E00-\u9FFF .()/+-]{2,40}\s*:\s+\S")
_BULLET_LINE_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+\S")
_KEY_VALUE_INLINE_RE = re.compile(r"\b[\w\u00C0-\u024F\u4E00-\u9FFF _/-]{2,32}\s*:\s*[^:\n]{2,}")
_XMLISH_RE = re.compile(r"</?[A-Za-z][^>]{0,80}>")
_WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\r?\n(?P<body>[\s\S]*?)\r?\n```\s*$", re.IGNORECASE)


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteValueError(ValueError):
    pass


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _template_control_count(text: str) -> int:
    return len(_ARGUMENT_RE.findall(text)) + len(_MUSTACHE_RE.findall(text))


def _cjk_char_count(text: str) -> int:
    return len(_CJK_RE.findall(text))


def _json_metrics(value: Any) -> tuple[int, int]:
    container_count = 0
    scalar_count = 0
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            container_count += 1
            stack.extend(current.values())
        elif isinstance(current, list):
            container_count += 1
            stack.extend(current)
        else:
            scalar_count += 1
    return container_count, scalar_count


def _named_leaf_controls(value: Any, current_key: str | None = None) -> tuple[int, int]:
    count = 0
    nonempty_string_count = 0
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_count, nested_nonempty = _named_leaf_controls(nested, str(key))
            count += nested_count
            nonempty_string_count += nested_nonempty
        return count, nonempty_string_count
    if isinstance(value, list):
        for nested in value:
            nested_count, nested_nonempty = _named_leaf_controls(nested, current_key)
            count += nested_count
            nonempty_string_count += nested_nonempty
        return count, nonempty_string_count
    if not current_key:
        return 0, 0
    if value is None:
        return 0, 0
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return 0, 0
        return 1, 1
    return 1, 0


def _json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError("duplicate_keys")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise _NonFiniteValueError(value)


def _json_candidate(text: str) -> tuple[str | None, bool]:
    fenced = _JSON_FENCE_RE.match(text)
    if fenced:
        return fenced.group("body"), True
    stripped = text.strip()
    if stripped and stripped[0] in "[{":
        return stripped, False
    return None, False


def _strict_json_metadata(text: str) -> dict[str, Any] | None:
    candidate, fenced = _json_candidate(text)
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate, object_pairs_hook=_json_object_pairs, parse_constant=_reject_nonfinite)
    except _DuplicateKeyError:
        return {
            "tier": 4,
            "tier_label": TIER_LABELS[4],
            "label": TIER_LABELS[4],
            "variant": "invalid_json_duplicate_keys",
            "variant_rank": 0,
            "parse_status": "invalid_duplicate_keys",
            "reason": "invalid_json_duplicate_keys",
            "structural_score": 0,
            "reason_codes": ["invalid_json_duplicate_keys"],
            "signals": {
                "is_valid_json": False,
                "top_level_type": None,
                "top_level_key_count": 0,
                "json_container_count": 0,
                "json_scalar_count": 0,
                "useful_named_leaf_controls": 0,
                "nonempty_string_leaf_controls": 0,
                "template_control_count": _template_control_count(text),
                "section_line_count": 0,
                "key_value_inline_count": 0,
                "bullet_line_count": 0,
                "word_count": len(_WORD_RE.findall(text)),
                "cjk_char_count": _cjk_char_count(text),
                "fenced_json": fenced,
            },
        }
    except _NonFiniteValueError:
        return {
            "tier": 4,
            "tier_label": TIER_LABELS[4],
            "label": TIER_LABELS[4],
            "variant": "invalid_json_nonfinite",
            "variant_rank": 0,
            "parse_status": "invalid_nonfinite",
            "reason": "invalid_json_nonfinite",
            "structural_score": 0,
            "reason_codes": ["invalid_json_nonfinite"],
            "signals": {
                "is_valid_json": False,
                "top_level_type": None,
                "top_level_key_count": 0,
                "json_container_count": 0,
                "json_scalar_count": 0,
                "useful_named_leaf_controls": 0,
                "nonempty_string_leaf_controls": 0,
                "template_control_count": _template_control_count(text),
                "section_line_count": 0,
                "key_value_inline_count": 0,
                "bullet_line_count": 0,
                "word_count": len(_WORD_RE.findall(text)),
                "cjk_char_count": _cjk_char_count(text),
                "fenced_json": fenced,
            },
        }
    except json.JSONDecodeError:
        return {
            "tier": 4,
            "tier_label": TIER_LABELS[4],
            "label": TIER_LABELS[4],
            "variant": "invalid_json_syntax",
            "variant_rank": 0,
            "parse_status": "invalid_syntax",
            "reason": "invalid_json_syntax",
            "structural_score": 0,
            "reason_codes": ["invalid_json_syntax"],
            "signals": {
                "is_valid_json": False,
                "top_level_type": None,
                "top_level_key_count": 0,
                "json_container_count": 0,
                "json_scalar_count": 0,
                "useful_named_leaf_controls": 0,
                "nonempty_string_leaf_controls": 0,
                "template_control_count": _template_control_count(text),
                "section_line_count": 0,
                "key_value_inline_count": 0,
                "bullet_line_count": 0,
                "word_count": len(_WORD_RE.findall(text)),
                "cjk_char_count": _cjk_char_count(text),
                "fenced_json": fenced,
            },
        }
    if not isinstance(parsed, (dict, list)):
        return None
    top_level_type = "object" if isinstance(parsed, dict) else "array"
    top_level_key_count = len(parsed) if isinstance(parsed, dict) else len(parsed)
    container_count, scalar_count = _json_metrics(parsed)
    useful_controls, nonempty_string_controls = _named_leaf_controls(parsed)
    template_controls = _template_control_count(text)
    if useful_controls < 2:
        if top_level_key_count == 0:
            variant = "valid_json_empty_container"
            parse_status = "valid_empty"
            reason = "valid_json_empty_container"
        else:
            variant = "valid_json_thin_wrapper"
            parse_status = "valid_thin_wrapper"
            reason = "valid_json_thin_wrapper"
        return {
            "tier": 4,
            "tier_label": TIER_LABELS[4],
            "label": TIER_LABELS[4],
            "variant": variant,
            "variant_rank": 0,
            "parse_status": parse_status,
            "reason": reason,
            "structural_score": 0,
            "reason_codes": [reason, "heuristic_not_semantic_quality"],
            "signals": {
                "is_valid_json": True,
                "top_level_type": top_level_type,
                "top_level_key_count": top_level_key_count,
                "json_container_count": container_count,
                "json_scalar_count": scalar_count,
                "useful_named_leaf_controls": useful_controls,
                "nonempty_string_leaf_controls": nonempty_string_controls,
                "template_control_count": template_controls,
                "section_line_count": 0,
                "key_value_inline_count": 0,
                "bullet_line_count": 0,
                "word_count": len(_WORD_RE.findall(text)),
                "cjk_char_count": _cjk_char_count(text),
                "fenced_json": fenced,
            },
        }
    variant = "strict_json_template" if template_controls else "strict_json"
    variant_rank = 0 if template_controls else 1
    structural_score = (
        min(top_level_key_count, 12) * 5
        + min(container_count, 12) * 2
        + min(scalar_count, 24)
        + min(useful_controls, 24)
        + min(template_controls * 3, 18)
    )
    return {
        "tier": 1,
        "tier_label": TIER_LABELS[1],
        "label": TIER_LABELS[1],
        "variant": variant,
        "variant_rank": variant_rank,
        "parse_status": "valid",
        "reason": variant,
        "structural_score": structural_score,
        "reason_codes": ["valid_json_container", "top_level_" + top_level_type]
        + (["contains_template_controls"] if template_controls else []),
        "signals": {
            "is_valid_json": True,
            "top_level_type": top_level_type,
            "top_level_key_count": top_level_key_count,
            "json_container_count": container_count,
            "json_scalar_count": scalar_count,
            "useful_named_leaf_controls": useful_controls,
            "nonempty_string_leaf_controls": nonempty_string_controls,
            "template_control_count": template_controls,
            "section_line_count": 0,
            "key_value_inline_count": 0,
            "bullet_line_count": 0,
            "word_count": len(_WORD_RE.findall(text)),
            "cjk_char_count": _cjk_char_count(text),
            "fenced_json": fenced,
        },
    }


def rank_prompt(text: str | None) -> dict[str, Any]:
    prompt = _text(text)
    stripped = prompt.strip()
    if not stripped:
        return {
            "tier": 4,
            "tier_label": TIER_LABELS[4],
            "label": TIER_LABELS[4],
            "variant": "empty",
            "variant_rank": 1,
            "parse_status": "empty",
            "reason": "empty",
            "structural_score": 0,
            "reason_codes": ["empty_prompt"],
            "signals": {
                "is_valid_json": False,
                "top_level_type": None,
                "top_level_key_count": 0,
                "json_container_count": 0,
                "json_scalar_count": 0,
                "useful_named_leaf_controls": 0,
                "nonempty_string_leaf_controls": 0,
                "template_control_count": 0,
                "section_line_count": 0,
                "key_value_inline_count": 0,
                "bullet_line_count": 0,
                "word_count": 0,
                "cjk_char_count": 0,
                "fenced_json": False,
            },
        }

    strict_json = _strict_json_metadata(prompt)
    if strict_json is not None:
        return strict_json

    parse_status = "invalid" if stripped[0] in "[{" else "not_json"

    section_lines = len(_SECTION_LINE_RE.findall(prompt))
    bullet_lines = len(_BULLET_LINE_RE.findall(prompt))
    key_value_inline = len(_KEY_VALUE_INLINE_RE.findall(prompt))
    template_controls = _template_control_count(prompt)
    xmlish = len(_XMLISH_RE.findall(prompt))
    word_count = len(_WORD_RE.findall(prompt))
    cjk_char_count = _cjk_char_count(prompt)
    newline_count = prompt.count("\n")
    sentence_markers = sum(prompt.count(marker) for marker in (".", "。", "!", "！", "?", "？"))

    structured_controls = (
        section_lines >= 2
        or bullet_lines >= 2
        or key_value_inline >= 3
        or (template_controls >= 1 and (section_lines >= 1 or newline_count >= 1 or key_value_inline >= 1 or ":" in prompt))
        or xmlish >= 2
    )
    if structured_controls:
        if template_controls >= 1 and (section_lines >= 1 or key_value_inline >= 1 or ":" in prompt):
            variant = "sectioned_template_controls"
            variant_rank = 0
            reason_codes = ["contains_template_controls", "explicit_sections_or_key_values"]
        elif section_lines >= 2:
            variant = "explicit_sections"
            variant_rank = 1
            reason_codes = ["explicit_sections"]
        elif key_value_inline >= 3:
            variant = "key_value_controls"
            variant_rank = 2
            reason_codes = ["repeated_key_value_controls"]
        elif bullet_lines >= 2:
            variant = "bullet_structured"
            variant_rank = 3
            reason_codes = ["bullet_structure"]
        else:
            variant = "template_controls"
            variant_rank = 4
            reason_codes = ["contains_template_controls"]
        return {
            "tier": 2,
            "tier_label": TIER_LABELS[2],
            "label": TIER_LABELS[2],
            "variant": variant,
            "variant_rank": variant_rank,
            "parse_status": parse_status,
            "reason": variant,
            "structural_score": section_lines * 4 + key_value_inline * 3 + bullet_lines * 2 + min(template_controls * 3, 18) + min(xmlish, 6),
            "reason_codes": reason_codes,
            "signals": {
                "is_valid_json": False,
                "top_level_type": None,
                "top_level_key_count": 0,
                "json_container_count": 0,
                "json_scalar_count": 0,
                "useful_named_leaf_controls": 0,
                "nonempty_string_leaf_controls": 0,
                "template_control_count": template_controls,
                "section_line_count": section_lines,
                "key_value_inline_count": key_value_inline,
                "bullet_line_count": bullet_lines,
                "word_count": word_count,
                "cjk_char_count": cjk_char_count,
                "fenced_json": False,
            },
        }

    if word_count >= 8 or sentence_markers >= 1 or cjk_char_count >= 12:
        return {
            "tier": 3,
            "tier_label": TIER_LABELS[3],
            "label": TIER_LABELS[3],
            "variant": "descriptive_natural_language",
            "variant_rank": 0,
            "parse_status": parse_status,
            "reason": "descriptive_natural_language",
            "structural_score": min(sentence_markers, 8),
            "reason_codes": ["descriptive_natural_language"],
            "signals": {
                "is_valid_json": False,
                "top_level_type": None,
                "top_level_key_count": 0,
                "json_container_count": 0,
                "json_scalar_count": 0,
                "useful_named_leaf_controls": 0,
                "nonempty_string_leaf_controls": 0,
                "template_control_count": template_controls,
                "section_line_count": section_lines,
                "key_value_inline_count": key_value_inline,
                "bullet_line_count": bullet_lines,
                "word_count": word_count,
                "cjk_char_count": cjk_char_count,
                "fenced_json": False,
            },
        }

    return {
        "tier": 4,
        "tier_label": TIER_LABELS[4],
        "label": TIER_LABELS[4],
        "variant": "minimal_text",
        "variant_rank": 0,
        "parse_status": parse_status,
        "reason": "minimal_text",
        "structural_score": 0,
        "reason_codes": ["minimal_text"],
        "signals": {
            "is_valid_json": False,
            "top_level_type": None,
            "top_level_key_count": 0,
            "json_container_count": 0,
            "json_scalar_count": 0,
            "useful_named_leaf_controls": 0,
            "nonempty_string_leaf_controls": 0,
            "template_control_count": template_controls,
            "section_line_count": section_lines,
            "key_value_inline_count": key_value_inline,
            "bullet_line_count": bullet_lines,
            "word_count": word_count,
            "cjk_char_count": cjk_char_count,
            "fenced_json": False,
        },
    }


def priority_sort_key(priority: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(priority.get("tier", 9)),
        int(priority.get("variant_rank", 99)),
        -int(priority.get("structural_score", 0)),
    )
