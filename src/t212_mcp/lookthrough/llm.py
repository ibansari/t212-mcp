"""The only module that knows about the LLM provider (OpenAI). Everything else gets a LangChain chat model."""

from typing import TypeVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from ..config import Settings

T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(Exception):
    pass


class TokenBudget(BaseCallbackHandler):
    """Counts tokens across every LLM call in a fund run and aborts when the budget is spent."""

    raise_error = True

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def on_llm_end(self, response, **kwargs) -> None:
        for generations in response.generations:
            for g in generations:
                usage = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                self.used += usage.get("total_tokens", 0)
        if self.used > self.limit:
            raise BudgetExceeded(f"token budget exhausted ({self.used} > {self.limit})")


def chat_model(settings: Settings) -> BaseChatModel:
    if settings.openai_api_key is not None:
        return ChatOpenAI(model=settings.llm_model, api_key=settings.openai_api_key, **settings.llm_kwargs)
    return ChatOpenAI(model=settings.llm_model, **settings.llm_kwargs)  # falls back to OPENAI_API_KEY in the environment


def structured(model: BaseChatModel, schema: type[T]):
    """Structured output via OpenAI's native JSON-schema mode, falling back to the default method."""
    try:
        return model.with_structured_output(schema, method="json_schema")
    except (TypeError, ValueError, NotImplementedError):
        return model.with_structured_output(schema)
