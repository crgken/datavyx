"""Chat Completions compatibility for explicitly configured non-reasoning calls."""
import os

from openai import AsyncOpenAI

from .model import require


def reasoning_settings():
    effort = os.getenv("LLM_REASONING_EFFORT", "")
    require(effort in {"", "none"}, "invalid_model_configuration",
            "当前 Chat Completions 工具链只支持显式关闭推理；使用推理需要迁移 Responses API。", 503)
    return {"openai_reasoning_effort": effort} if effort else {}


class ChatCompletionsClient(AsyncOpenAI):
    """Adapt Graphiti's legacy parameters at the application SDK boundary."""

    def __init__(self, *, reasoning_effort=None, **kwargs):
        super().__init__(**kwargs)
        self.reasoning_effort = reasoning_effort

    async def post(self, path, **kwargs):
        if path == "/chat/completions" and self.reasoning_effort is not None:
            body = dict(kwargs["body"])
            body["reasoning_effort"] = self.reasoning_effort
            # Graphiti's old True/False token biases are unsupported by Luna.
            body.pop("logit_bias", None)
            if "max_tokens" in body:
                # Graphiti's reranker asks for one token. Reserve room for the
                # completion terminator so Luna can finish that short answer.
                body.setdefault("max_completion_tokens", max(16, body.pop("max_tokens")))
            kwargs["body"] = body
        return await super().post(path, **kwargs)
