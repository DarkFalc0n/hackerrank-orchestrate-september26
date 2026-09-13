"""Load the request analyser JSONC contracts into strict Pydantic models.

``input_schema.jsonc`` describes one request joined with the user's preferences
and the engine's candidate plans. ``output_schema.jsonc`` describes the single
``output.csv`` row the model must return. Both files are the source of truth:
this module strips their comments, cross-checks their enum fields against the
shared ingest enums, and builds the models used for validation and structured
output.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model

from ...ingest.models import (
    AffordabilityStatus,
    Currency,
    PaymentMethod,
    RequestType,
)

BASE_DIR = Path(__file__).resolve().parent
INPUT_SCHEMA_FILE = BASE_DIR / "input_schema.jsonc"
OUTPUT_SCHEMA_FILE = BASE_DIR / "output_schema.jsonc"
SYSTEM_PROMPT_FILE = BASE_DIR / "system_prompt.md"

INPUT_ENUM_FIELDS: dict[str, type[Enum]] = {
    "request_type": RequestType,
    "home_currency": Currency,
    "payment_method": PaymentMethod,
    "affordability_status": AffordabilityStatus,
}

OUTPUT_ENUM_FIELDS: dict[str, type[Enum]] = {
    "affordability_status": AffordabilityStatus,
    "recommended_payment_method": PaymentMethod,
}

_TYPE_MAP: dict[str, type] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
}


def strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments while preserving string literals."""
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while (
                index + 1 < length
                and not (text[index] == "*" and text[index + 1] == "/")
            ):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def load_schema(path: Path) -> dict[str, Any]:
    """Return a JSONC schema file as a plain dictionary."""
    return json.loads(strip_jsonc(path.read_text(encoding="utf-8")))


def load_input_schema() -> dict[str, Any]:
    return load_schema(INPUT_SCHEMA_FILE)


def load_output_schema() -> dict[str, Any]:
    return load_schema(OUTPUT_SCHEMA_FILE)


def load_system_prompt() -> str:
    """Return the request analyser system prompt."""
    return SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()


def _declared_types(spec: dict[str, Any]) -> list[str]:
    declared = spec.get("type")
    if isinstance(declared, str):
        return [declared]
    return list(declared or [])


def _is_nullable(spec: dict[str, Any]) -> bool:
    return "null" in _declared_types(spec)


def _enum_base(
    field_name: str, spec: dict[str, Any], enum_fields: dict[str, type[Enum]]
) -> Any:
    values = [value for value in spec["enum"] if value is not None]
    expected = enum_fields.get(field_name)
    if expected is None:
        return Literal[tuple(values)]  # type: ignore[valid-type]
    known = {member.value for member in expected}
    unknown = set(values) - known
    if unknown:
        raise ValueError(
            f"schema field '{field_name}' has values outside {expected.__name__}: "
            f"{sorted(unknown)}"
        )
    missing = known - set(values)
    if missing:
        raise ValueError(
            f"schema field '{field_name}' is missing {expected.__name__} values: "
            f"{sorted(missing)}"
        )
    return expected


def _model_name(prefix: str, field_name: str) -> str:
    return prefix + "".join(part.capitalize() for part in field_name.split("_"))


def _base_annotation(
    field_name: str, spec: dict[str, Any], enum_fields: dict[str, type[Enum]]
) -> Any:
    if spec.get("enum"):
        return _enum_base(field_name, spec, enum_fields)
    fmt = spec.get("format")
    if fmt == "date":
        return date
    if fmt == "date-time":
        return datetime
    types = [name for name in _declared_types(spec) if name != "null"]
    if not types:
        return Any
    if types[0] == "array":
        item_spec = spec.get("items", {})
        item = _annotation(field_name, item_spec, enum_fields)
        return list[item]
    if types[0] == "object" or "properties" in spec:
        return _object_model(spec, enum_fields, _model_name("", field_name) + "Item")
    return _TYPE_MAP.get(types[0], str)


def _annotation(
    field_name: str, spec: dict[str, Any], enum_fields: dict[str, type[Enum]]
) -> Any:
    base = _base_annotation(field_name, spec, enum_fields)
    if _is_nullable(spec):
        return Optional[base]
    return base


def _field_definitions(
    properties: dict[str, Any], enum_fields: dict[str, type[Enum]]
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for field_name, spec in properties.items():
        nullable = _is_nullable(spec)
        fields[field_name] = (
            _annotation(field_name, spec, enum_fields),
            Field(
                default=None if nullable else ...,
                description=spec.get("description"),
            ),
        )
    return fields


def _object_model(
    schema: dict[str, Any], enum_fields: dict[str, type[Enum]], name: str
) -> type[BaseModel]:
    properties: dict[str, Any] = schema.get("properties", {})
    if not properties:
        raise ValueError(f"schema object '{name}' declares no properties")
    return create_model(
        name,
        __config__=ConfigDict(extra="forbid"),
        **_field_definitions(properties, enum_fields),
    )


def build_model(
    schema: dict[str, Any],
    enum_fields: dict[str, type[Enum]],
    name: str,
) -> type[BaseModel]:
    """Create the strict Pydantic model described by ``schema``."""
    properties: dict[str, Any] = schema.get("properties", {})
    if not properties:
        raise ValueError("schema declares no properties")
    return _object_model(schema, enum_fields, name)


@lru_cache(maxsize=1)
def get_input_model() -> type[BaseModel]:
    """Cached input model built from ``input_schema.jsonc``."""
    return build_model(load_input_schema(), INPUT_ENUM_FIELDS, "RequestAnalyserInput")


@lru_cache(maxsize=1)
def get_output_model() -> type[BaseModel]:
    """Cached output model built from ``output_schema.jsonc``."""
    return build_model(load_output_schema(), OUTPUT_ENUM_FIELDS, "RequestAnalyserOutput")


__all__ = [
    "INPUT_ENUM_FIELDS",
    "INPUT_SCHEMA_FILE",
    "OUTPUT_ENUM_FIELDS",
    "OUTPUT_SCHEMA_FILE",
    "SYSTEM_PROMPT_FILE",
    "build_model",
    "get_input_model",
    "get_output_model",
    "load_input_schema",
    "load_output_schema",
    "load_schema",
    "load_system_prompt",
    "strip_jsonc",
]
