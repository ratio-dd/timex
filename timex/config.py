"""Explicit live opt-in and provider-independent execution limits."""
import math
import os
from dataclasses import dataclass, fields
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Limits:
    max_model_calls: int = 10
    call_timeout_seconds: float = 60
    total_timeout_seconds: float = 300
    max_input_characters: int = 30000
    max_output_characters: int = 12000
    max_input_tokens: int = 16000
    max_output_tokens: int = 1600
    max_total_tokens: int = 120000
    max_estimated_usd_per_run: float = 0.25
    max_retries: int = 1

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if not math.isfinite(value) or value < 0 or (value == 0 and field.name != "max_retries"):
                raise ValueError("limits must be finite and positive")
            if field.type is int and (type(value) is not int):
                raise ValueError("count limits must be integers")
        if not 1 <= self.max_model_calls <= 10 or self.max_retries not in (0, 1):
            raise ValueError("at most 10 calls and one shared retry per run")

    @classmethod
    def from_env(cls):
        return cls(**{field.name: field.type(os.environ[field.name.upper()])
                      for field in fields(cls) if field.name.upper() in os.environ})


@dataclass(frozen=True)
class ModelSettings:
    provider: str = "fake"
    model_name: str | None = None
    base_url: str | None = None
    thinking: str | None = None
    output_mode: str = 'json_object'
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None

    def __post_init__(self):
        if self.provider not in ("fake", "openai"):
            raise ValueError("MODEL_PROVIDER must be fake or openai")
        if self.output_mode not in ('json_object', 'deepseek_strict_tools'):
            raise ValueError('unsupported model output mode')
        if self.output_mode == 'deepseek_strict_tools' and self.base_url != 'https://api.deepseek.com/beta':
            raise ValueError('DeepSeek strict tools require the documented beta endpoint')
        if self.thinking not in (None, "enabled", "disabled"):
            raise ValueError("MODEL_THINKING must be enabled or disabled when configured")
        if self.provider == "openai":
            if not self.model_name or not self.model_name.strip():
                raise ValueError("MODEL_NAME is required")
            for price in (self.input_usd_per_million, self.output_usd_per_million):
                if price is None or not math.isfinite(price) or price < 0:
                    raise ValueError("confirmed finite input/output model prices are required")
            if self.base_url:
                url = urlsplit(self.base_url)
                if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                    raise ValueError("OPENAI_BASE_URL must be an HTTPS endpoint without credentials")

    def authorize(self, live: bool):
        if (self.provider != "fake") != live:
            raise ValueError("live providers require --live; fake runs must omit --live")

    @classmethod
    def from_env(cls, provider: str | None = None):
        provider = provider or os.getenv("MODEL_PROVIDER", "fake")
        if provider == "fake":
            return cls()
        def price(name):
            value = os.getenv(name)
            return float(value) if value is not None else None
        return cls(provider=provider, model_name=os.getenv("MODEL_NAME"),
                   base_url=os.getenv("OPENAI_BASE_URL"),
                   thinking=os.getenv("MODEL_THINKING"),
                   output_mode=os.getenv('MODEL_OUTPUT_MODE', 'json_object'),
                   input_usd_per_million=price("MODEL_INPUT_USD_PER_MILLION"),
                   output_usd_per_million=price("MODEL_OUTPUT_USD_PER_MILLION"))

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        if self.provider == "fake":
            return 0.0
        return (input_tokens * self.input_usd_per_million +
                output_tokens * self.output_usd_per_million) / 1_000_000
