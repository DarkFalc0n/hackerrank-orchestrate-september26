"""Async OpenAI connector with Pydantic-structured text and image generation.

The connector wraps ``AsyncOpenAI`` and exposes a small, typed surface:

* :meth:`OpenAIConnector.generate` for plain (async) text generation.
* :meth:`OpenAIConnector.generate_structured` for *strict* structured output
  parsed into a caller-supplied Pydantic model.
* Image-aware variants that accept local files or remote URLs alongside text.

Every call returns token usage so the evaluation usage report can be produced
without extra instrumentation.
"""

from __future__ import annotations

import base64
import mimetypes
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Iterable, Sequence, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import ConnectorConfigError, LLMSettings

ResponseT = TypeVar("ResponseT", bound=BaseModel)

DEFAULT_IMAGE_MIME = "image/png"


class ImageDetail(str, Enum):
    """Vision resolution hint forwarded to the OpenAI API."""

    auto = "auto"
    low = "low"
    high = "high"


class ImageInput(BaseModel):
    """A single image input, either a local path or an ``http(s)`` URL."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    detail: ImageDetail = ImageDetail.auto

    @classmethod
    def from_path(
        cls, path: str | Path, detail: ImageDetail = ImageDetail.auto
    ) -> "ImageInput":
        return cls(source=str(path), detail=detail)

    @classmethod
    def from_url(
        cls, url: str, detail: ImageDetail = ImageDetail.auto
    ) -> "ImageInput":
        return cls(source=url, detail=detail)

    @model_validator(mode="after")
    def _validate_source(self) -> "ImageInput":
        if self.is_url:
            if not self.source.lower().startswith(("http://", "https://")):
                raise ValueError(f"invalid image URL: {self.source!r}")
        else:
            if not Path(self.source).is_file():
                raise ValueError(f"image file not found: {self.source!r}")
        return self

    @property
    def is_url(self) -> bool:
        return self.source.lower().startswith(("http://", "https://", "data:"))

    def to_data_url(self) -> str:
        """Return a URL the API accepts, base64-encoding local files."""
        if self.is_url:
            return self.source
        path = Path(self.source)
        mime = mimetypes.guess_type(path.name)[0] or DEFAULT_IMAGE_MIME
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{payload}"

    def to_content_part(self) -> dict[str, Any]:
        return {
            "type": "image_url",
            "image_url": {"url": self.to_data_url(), "detail": self.detail.value},
        }


class UsageStats(BaseModel):
    """Token accounting for a single completion."""

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class TextResult(BaseModel):
    """Result of a plain text generation call."""

    model_config = ConfigDict(extra="forbid")

    text: str
    model: str
    finish_reason: str | None = None
    usage: UsageStats = Field(default_factory=UsageStats)


class StructuredResult(BaseModel, Generic[ResponseT]):
    """Result of a strict structured generation call."""

    model_config = ConfigDict(extra="forbid")

    parsed: ResponseT
    raw_text: str | None = None
    model: str
    finish_reason: str | None = None
    usage: UsageStats = Field(default_factory=UsageStats)


class StructuredGenerationError(RuntimeError):
    """Raised when the model refuses or fails to produce a parseable object."""


def _usage_from(response: Any) -> UsageStats:
    usage = getattr(response, "usage", None)
    if usage is None:
        return UsageStats()
    return UsageStats(
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        total_tokens=getattr(usage, "total_tokens", 0) or 0,
    )


def _finish_reason(response: Any) -> str | None:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    reason = getattr(choices[0], "finish_reason", None)
    return str(reason) if reason is not None else None


class OpenAIConnector:
    """Async OpenAI client returning validated text and Pydantic objects."""

    def __init__(
        self,
        settings: LLMSettings | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._settings = settings or LLMSettings()
        self._client = client or self._build_client()

    @property
    def settings(self) -> LLMSettings:
        return self._settings

    def _build_client(self) -> AsyncOpenAI:
        api_key = self._settings.resolve_api_key()
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": self._settings.timeout,
            "max_retries": self._settings.max_retries,
        }
        if self._settings.base_url:
            kwargs["base_url"] = self._settings.base_url
        if self._settings.organization:
            kwargs["organization"] = self._settings.organization
        return AsyncOpenAI(**kwargs)

    async def aclose(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> "OpenAIConnector":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _build_messages(
        self,
        prompt: str,
        system: str | None,
        images: Sequence[ImageInput] | None,
    ) -> list[dict[str, Any]]:
        if not prompt or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        if images:
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            content.extend(image.to_content_part() for image in images)
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": prompt})
        return messages

    def _request_kwargs(
        self,
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
        structured: bool,
        has_images: bool = False,
    ) -> dict[str, Any]:
        return {
            "model": model or self._settings.model_for(has_images),
            "temperature": (
                temperature
                if temperature is not None
                else self._settings.temperature_for(structured)
            ),
            "max_tokens": max_tokens or self._settings.max_tokens,
            "top_p": self._settings.top_p,
        }

    @staticmethod
    def _coerce_images(
        images: Iterable[ImageInput | str | Path] | None,
    ) -> list[ImageInput]:
        if not images:
            return []
        coerced: list[ImageInput] = []
        for image in images:
            if isinstance(image, ImageInput):
                coerced.append(image)
            else:
                coerced.append(ImageInput(source=str(image)))
        return coerced

    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: Iterable[ImageInput | str | Path] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Generate plain text, optionally grounded in one or more images."""
        result = await self.generate_detailed(
            prompt,
            system=system,
            images=images,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return result.text

    async def generate_detailed(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: Iterable[ImageInput | str | Path] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> TextResult:
        """Generate text and return content plus token usage metadata."""
        image_inputs = self._coerce_images(images)
        messages = self._build_messages(prompt, system, image_inputs)
        response = await self._client.chat.completions.create(
            messages=messages,  # type: ignore[arg-type]
            **self._request_kwargs(
                model,
                temperature,
                max_tokens,
                structured=False,
                has_images=bool(image_inputs),
            ),
        )
        choice = response.choices[0]
        return TextResult(
            text=choice.message.content or "",
            model=response.model,
            finish_reason=_finish_reason(response),
            usage=_usage_from(response),
        )

    async def generate_structured(
        self,
        prompt: str,
        response_model: type[ResponseT],
        *,
        system: str | None = None,
        images: Iterable[ImageInput | str | Path] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ResponseT:
        """Generate a strict structured object validated by ``response_model``."""
        result = await self.generate_structured_detailed(
            prompt,
            response_model,
            system=system,
            images=images,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return result.parsed

    async def generate_structured_detailed(
        self,
        prompt: str,
        response_model: type[ResponseT],
        *,
        system: str | None = None,
        images: Iterable[ImageInput | str | Path] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> StructuredResult[ResponseT]:
        """Like :meth:`generate_structured` but also returns usage metadata."""
        image_inputs = self._coerce_images(images)
        messages = self._build_messages(prompt, system, image_inputs)
        response = await self._client.chat.completions.parse(
            messages=messages,  # type: ignore[arg-type]
            response_format=response_model,
            **self._request_kwargs(
                model,
                temperature,
                max_tokens,
                structured=True,
                has_images=bool(image_inputs),
            ),
        )
        choice = response.choices[0]
        message = choice.message
        refusal = getattr(message, "refusal", None)
        parsed = message.parsed
        if parsed is None:
            detail = refusal or "model did not return a parseable object"
            raise StructuredGenerationError(detail)
        return StructuredResult[ResponseT](
            parsed=parsed,
            raw_text=message.content,
            model=response.model,
            finish_reason=_finish_reason(response),
            usage=_usage_from(response),
        )

    async def generate_from_images(
        self,
        prompt: str,
        images: Iterable[ImageInput | str | Path],
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Convenience wrapper for text generation grounded in images."""
        return await self.generate(
            prompt,
            system=system,
            images=images,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def generate_structured_from_images(
        self,
        prompt: str,
        images: Iterable[ImageInput | str | Path],
        response_model: type[ResponseT],
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ResponseT:
        """Convenience wrapper for strict structured output grounded in images."""
        return await self.generate_structured(
            prompt,
            response_model,
            system=system,
            images=images,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )


__all__ = [
    "ConnectorConfigError",
    "ImageDetail",
    "ImageInput",
    "OpenAIConnector",
    "StructuredGenerationError",
    "StructuredResult",
    "TextResult",
    "UsageStats",
]
