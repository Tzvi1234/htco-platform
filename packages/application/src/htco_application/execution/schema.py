"""A small JSON Schema (2020-12) checker for action inputs and outputs (slice 5.12).

The application layer imports only the standard library (ER-ARCH-01), so the gateway validates
with this subset: `type` (object, string, integer, number, boolean, array, null), `properties`,
`required`, `additionalProperties: false`, `enum`, `minLength`, `maxLength`, `pattern`,
`minimum`, `maximum`, `items`, `maxItems`. `check_schema` refuses any other keyword, so a contract
never silently relies on a rule this checker does not apply.
"""

import re
from collections.abc import Mapping
from typing import Any

_KNOWN = frozenset(
    {
        "$schema", "$id", "title", "description", "type", "properties", "required",
        "additionalProperties", "enum", "minLength", "maxLength", "pattern", "minimum",
        "maximum", "items", "maxItems",
    }
)  # fmt: skip
_TYPES: Mapping[str, tuple[type, ...]] = {
    "object": (dict,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "null": (type(None),),
}


def check_schema(schema: Mapping[str, Any], path: str = "$") -> None:
    unknown = sorted(set(schema) - _KNOWN)
    if unknown:
        raise ValueError(f"{path}: unsupported schema keywords {unknown}")
    kind = schema.get("type")
    if kind is not None and kind not in _TYPES:
        raise ValueError(f"{path}: unsupported type {kind!r}")
    for name, child in schema.get("properties", {}).items():
        check_schema(child, f"{path}.{name}")
    if isinstance(schema.get("items"), Mapping):
        check_schema(schema["items"], f"{path}[]")


def _is_type(value: Any, kind: str) -> bool:
    if kind in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[kind])


def validate(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:  # noqa: PLR0912 - one branch per keyword
    """Every violation, each as `path: message`; never the value itself (it may be personal)."""
    kind = schema.get("type")
    if kind is not None and not _is_type(value, kind):
        return [f"{path}: expected {kind}"]
    problems: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: not one of the allowed values")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{path}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path}: too long")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            problems.append(f"{path}: does not match the expected pattern")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: above the maximum")
    if isinstance(value, list | tuple):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: too many items")
        if isinstance(schema.get("items"), Mapping):
            for index, item in enumerate(value):
                problems += validate(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        properties: Mapping[str, Any] = schema.get("properties", {})
        problems += [
            f"{path}.{name}: required" for name in schema.get("required", ()) if name not in value
        ]
        if schema.get("additionalProperties") is False:
            problems += [f"{path}.{name}: not allowed" for name in value if name not in properties]
        for name, child in properties.items():
            if name in value:
                problems += validate(value[name], child, f"{path}.{name}")
    return problems
