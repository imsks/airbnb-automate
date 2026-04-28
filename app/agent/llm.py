"""LLM provider abstraction — swap between OpenAI, Gemini, or Perplexity."""

from __future__ import annotations

import os
from functools import lru_cache

from langchain_core.language_models.chat_models import BaseChatModel


def _get_env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


@lru_cache(maxsize=1)
def get_llm(
    *,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> BaseChatModel:
    """Return a LangChain chat model for the configured (or given) provider.

    Supported providers:
      * **openai**     — requires ``OPENAI_API_KEY``
      * **gemini**     — requires ``GOOGLE_API_KEY``
      * **perplexity** — requires ``PERPLEXITY_API_KEY`` (uses OpenAI-compatible API)
    """
    provider = (provider or _get_env("LLM_PROVIDER", "openai")).lower()
    temp = temperature if temperature is not None else float(_get_env("LLM_TEMPERATURE", "0.7"))

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore[import-untyped]

        chosen_model = model or _get_env("GEMINI_MODEL", "gemini-2.5-flash")
        return ChatGoogleGenerativeAI(
            model=chosen_model,
            temperature=temp,
            google_api_key=_get_env("GOOGLE_API_KEY"),
        )

    if provider == "perplexity":
        from langchain_openai import ChatOpenAI

        chosen_model = model or _get_env("PERPLEXITY_MODEL", "sonar-pro")
        return ChatOpenAI(
            model=chosen_model,
            temperature=temp,
            openai_api_key=_get_env("PERPLEXITY_API_KEY"),
            openai_api_base="https://api.perplexity.ai",
        )

    # Default: OpenAI
    from langchain_openai import ChatOpenAI

    chosen_model = model or _get_env("OPENAI_MODEL", "gpt-4o-mini")
    return ChatOpenAI(
        model=chosen_model,
        temperature=temp,
        openai_api_key=_get_env("OPENAI_API_KEY"),
    )
