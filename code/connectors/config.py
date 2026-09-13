"""Environment-driven configuration for the LLM connectors.

Settings are loaded from process environment variables first and fall back to a
``.env`` file at the repository root. Every field is validated by Pydantic so a
bad temperature, timeout, or retry count fails fast at startup rather than
mid-run.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


class ConnectorConfigError(ValueError):
    """Raised when required connector configuration is missing or unusable."""


class LLMSettings(BaseSettings):
    """Validated OpenAI-compatible connection and generation settings.

    All values may be supplied via ``OPENAI_*`` environment variables or the
    repository ``.env`` file. Per-call overrides are accepted by the connector
    methods and never mutate this object.
    """

    model_config = SettingsConfigDict(
        env_prefix="OPENAI_",
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    api_key: SecretStr | None = None
    base_url: str | None = None
    organization: str | None = None

    model: str = "gpt-4o-mini"
    multimodal_model: str = "gpt-4o-mini"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    structured_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    max_tokens: int = Field(default=1024, gt=0)
    timeout: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0)

    @model_validator(mode="after")
    def _normalize(self) -> "LLMSettings":
        if self.base_url is not None:
            self.base_url = self.base_url.rstrip("/") or None
        if self.organization is not None:
            self.organization = self.organization or None
        return self

    def temperature_for(self, structured: bool) -> float:
        """Return the default temperature for the requested generation mode."""
        return self.structured_temperature if structured else self.temperature

    def model_for(self, has_images: bool) -> str:
        """Return the default model for text-only or image-bearing requests."""
        return self.multimodal_model if has_images else self.model

    def resolve_api_key(self) -> str:
        """Return the API key as plain text or raise a clear configuration error."""
        if self.api_key is None or not self.api_key.get_secret_value().strip():
            raise ConnectorConfigError(
                "OPENAI_API_KEY is not set. Add it to the environment or to "
                f"{ENV_FILE} (see .env.example)."
            )
        return self.api_key.get_secret_value()


def load_settings(**overrides: object) -> LLMSettings:
    """Load settings from the environment, applying optional explicit overrides."""
    return LLMSettings(**overrides)  # type: ignore[arg-type]
