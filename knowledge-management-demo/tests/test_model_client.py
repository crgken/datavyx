import json
import os
import unittest
from unittest.mock import patch

import httpx

from demo.model import DemoError
from demo.model_client import ChatCompletionsClient, reasoning_settings


class ModelClientTests(unittest.IsolatedAsyncioTestCase):
    async def capture(self, effort):
        captured = []

        async def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "test-completion", "object": "chat.completion", "created": 1,
                "model": "gpt-6-luna", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "OK"}}]})

        async with ChatCompletionsClient(
                api_key="test-only-not-a-real-key", reasoning_effort=effort,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
            await client.chat.completions.create(model="gpt-6-luna",
                messages=[{"role": "user", "content": "test"}], max_tokens=1,
                temperature=0, logprobs=True, top_logprobs=2, logit_bias={'6432': 1})
        return captured[0]

    async def test_luna_legacy_graphiti_request_parameters(self):
        body = await self.capture("none")
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["max_completion_tokens"], 16)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["temperature"], 0)
        self.assertTrue(body["logprobs"])
        self.assertNotIn("logit_bias", body)

    async def test_legacy_provider_request_is_preserved_without_opt_in(self):
        body = await self.capture(None)
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["max_tokens"], 1)
        self.assertNotIn("max_completion_tokens", body)
        self.assertEqual(body["logit_bias"], {'6432': 1})

    async def test_reasoning_settings_are_explicit(self):
        with patch.dict(os.environ, {"LLM_REASONING_EFFORT": "none"}):
            self.assertEqual(reasoning_settings(), {"openai_reasoning_effort": "none"})
        with patch.dict(os.environ, {"LLM_REASONING_EFFORT": ""}):
            self.assertEqual(reasoning_settings(), {})
        with patch.dict(os.environ, {"LLM_REASONING_EFFORT": "medium"}):
            with self.assertRaises(DemoError):
                reasoning_settings()
