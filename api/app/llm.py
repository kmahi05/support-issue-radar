"""Gemini model factories (§2 RAG). PRD "Haiku" -> GEMINI_MODEL, "Sonnet" -> GEMINI_MODEL_STRONG.

Tests inject langchain_core fakes with set_overrides(); they never call Gemini.
"""

from functools import cache

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from .config import get_settings

_overrides: dict[str, object] = {}


def set_overrides(
    *, chat: BaseChatModel | None = None, strong: BaseChatModel | None = None, embed=None
) -> None:
    """Replace the real models (tests only). Pass nothing to clear."""
    _overrides.clear()
    _overrides.update(
        {k: v for k, v in {"chat": chat, "strong": strong, "embed": embed}.items() if v}
    )


def chat_model(strong: bool = False) -> BaseChatModel:
    key = "strong" if strong else "chat"
    if key in _overrides:
        return _overrides[key]  # type: ignore[return-value]
    return _real_chat(strong)


def embeddings() -> Embeddings:
    if "embed" in _overrides:
        return _overrides["embed"]  # type: ignore[return-value]
    return _real_embeddings()


@cache
def _real_chat(strong: bool) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    s = get_settings()
    return ChatGoogleGenerativeAI(
        model=s.GEMINI_MODEL_STRONG if strong else s.GEMINI_MODEL, temperature=0
    )


@cache
def _real_embeddings() -> Embeddings:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    return GoogleGenerativeAIEmbeddings(model=get_settings().GEMINI_EMBEDDING_MODEL)
