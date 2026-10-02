"""JSON answers from a model without constrained decoding (the `local` backend): find the JSON object in the text,
and check it against the call's JSON schema.

The validator covers the subset of JSON Schema the pipeline's and the judge's schemas use: `type` (object, array,
string, integer, number, boolean), `properties`, `required`, `additionalProperties: false`, `enum`, `items` and
`minItems`. Anything else in a schema is ignored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract(text: str) -> Any:
    """The JSON value in a model's answer: the whole text, else a fenced block, else the first object that parses.
    Raises ValueError when there is none. A reasoning block (`<think>...</think>`) is skipped."""
    body = _THINK.sub("", text).strip()
    candidates = [body, *(m.group(1).strip() for m in _FENCE.finditer(body))]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", body):
        try:
            value, _ = decoder.raw_decode(body, match.start())
        except json.JSONDecodeError:
            continue
        return value
    raise ValueError("no JSON object in the answer")


_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,), "array": (list,), "string": (str,), "boolean": (bool,), "integer": (int,),
    "number": (int, float),
}  # fmt: skip


def errors(value: Any, schema: Mapping[str, Any], where: str = "$") -> list[str]:
    """What makes `value` fail `schema` (empty when it passes)."""
    kind = schema.get("type")
    if isinstance(kind, str) and kind in _TYPES:
        wrong = not isinstance(value, _TYPES[kind]) or (kind in ("integer", "number") and isinstance(value, bool))
        if wrong:
            return [f"{where}: expected {kind}, got {type(value).__name__}"]
    out: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: {value!r} is not one of {list(schema['enum'])}")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        out += [f"{where}: missing `{key}`" for key in schema.get("required") or () if key not in value]
        if schema.get("additionalProperties") is False:
            out += [f"{where}: unexpected `{key}`" for key in value if key not in properties]
        for key, sub in properties.items():
            if key in value:
                out += errors(value[key], sub, f"{where}.{key}")
    if isinstance(value, list):
        if len(value) < int(schema.get("minItems") or 0):
            out.append(f"{where}: at least {schema['minItems']} items")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for n, item in enumerate(value):
                out += errors(item, items, f"{where}[{n}]")
    return out


def skeleton(schema: Mapping[str, Any]) -> Any:
    """An example value of the schema's shape: the first enum value, "..." for free text, one item per array."""
    if schema.get("enum"):
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {key: skeleton(sub) for key, sub in (schema.get("properties") or {}).items()}
    if kind == "array":
        items = schema.get("items")
        return [skeleton(items)] if isinstance(items, Mapping) else []
    return {"string": "...", "boolean": True, "integer": 0, "number": 0.5}.get(str(kind))


def instructions(schema: Mapping[str, Any]) -> str:
    """The answer-format section appended to the system prompt: the schema, and an example of the answer's shape
    (models without constrained decoding otherwise tend to echo the schema back)."""
    return (
        "## Answer format\n\nAnswer with one JSON object and nothing else: no prose before or after it, no code "
        "fence. It must match this JSON Schema (the schema describes your answer; don't repeat it):\n"
        + json.dumps(schema, separators=(",", ":"))
        + "\n\nThe shape of an answer, with placeholder values:\n"
        + json.dumps(skeleton(schema), separators=(",", ":"))
    )
