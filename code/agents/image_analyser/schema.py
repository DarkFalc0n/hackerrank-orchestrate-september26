"""Load the image analyser's JSONC contract into a strict Pydantic model.

``output_schema.jsonc`` is the single source of truth for the analyser output.
This module strips its comments and builds the Pydantic model that is passed to
the OpenAI structured-output call. The contract is intentionally minimal: an
image only supplies the event's missing ``amount`` plus a short summary.
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model

BASE_DIR = Path(__file__).resolve().parent
SCHEMA_FILE = BASE_DIR / "output_schema.jsonc"
SYSTEM_PROMPT_FILE = BASE_DIR / "system_prompt.md"

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


def load_schema() -> dict[str, Any]:
    """Return the parsed JSONC schema as a plain dictionary."""
    return json.loads(strip_jsonc(SCHEMA_FILE.read_text(encoding="utf-8")))


def load_system_prompt() -> str:
    """Return the analyser system prompt."""
    return SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()


def _annotation(field_name: str, spec: dict[str, Any]) -> Any:
    if spec.get("format") == "date":
        return Optional[date]
    declared = spec.get("type")
    types = [declared] if isinstance(declared, str) else list(declared or [])
    types = [name for name in types if name != "null"]
    if not types:
        return Optional[Any]
    return Optional[_TYPE_MAP.get(types[0], str)]


def build_response_model(
    schema: dict[str, Any] | None = None, name: str = "ImageAnalysis"
) -> type[BaseModel]:
    """Create the strict Pydantic model described by ``schema``."""
    schema = schema or load_schema()
    properties: dict[str, Any] = schema.get("properties", {})
    if not properties:
        raise ValueError("output schema declares no properties")
    fields: dict[str, Any] = {}
    for field_name, spec in properties.items():
        fields[field_name] = (
            _annotation(field_name, spec),
            Field(default=None, description=spec.get("description")),
        )
    model = create_model(
        name,
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return model


@lru_cache(maxsize=1)
def get_response_model() -> type[BaseModel]:
    """Cached response model built from the on-disk schema."""
    return build_response_model()


__all__ = [
    "SCHEMA_FILE",
    "SYSTEM_PROMPT_FILE",
    "build_response_model",
    "get_response_model",
    "load_schema",
    "load_system_prompt",
    "strip_jsonc",
]
