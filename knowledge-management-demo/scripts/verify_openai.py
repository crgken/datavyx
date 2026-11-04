"""Real OpenAI + mobile gateway acceptance on disposable test Neo4j ports only.

Exercises the backend flow; does not impersonate a real Auth0 browser login.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from demo.config import Config, load_model_env
from demo.diagnostics import log_failure
from demo.graphiti_extension import Pipeline
from demo.mobile_auth import MobileAuth
from demo.mobile_gateway import MobileGateway
from demo.model_client import ChatCompletionsClient, reasoning_settings
from demo.store import Store


CLIENTS = [
    ("Johnson Logistics", "Chicago", "Emily Carter", "logistics"),
    ("NorthPeak Retail", "Seattle", "Daniel Brooks", "retail"),
    ("BrightPath Health", "Austin", "Sophia Turner", "healthcare"),
    ("BlueRock Manufacturing", "Denver", "Michael Reed", "manufacturing"),
    ("ClearWave Media", "Boston", "Olivia Scott", "media"),
]
MESSAGE = "\n\n".join(
    f"Client {name} is based in {city}. Main contact is {person}. Industry is {industry}."
    for name, city, person, industry in CLIENTS)


def candidate_configuration(path, active_path=None):
    candidate = dotenv_values(path)
    if not candidate.get("LLM_API_KEY"):
        raise ValueError("Candidate LLM_API_KEY is missing")
    if candidate.get("LLM_BASE_URL") != "https://api.openai.com/v1":
        raise ValueError("Candidate must use the official OpenAI API endpoint")
    if not candidate.get("LLM_MODEL"):
        raise ValueError("Candidate LLM_MODEL is missing")
    if candidate["LLM_MODEL"] != "gpt-6-luna" or candidate.get("LLM_REASONING_EFFORT") != "none":
        raise ValueError("Candidate must explicitly select gpt-6-luna with reasoning disabled")
    if active_path:
        load_model_env(active_path)
    # The candidate changes only LLM settings. Existing embedding settings stay separate.
    for field in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_REASONING_EFFORT"):
        os.environ[field] = candidate[field]
    return candidate


async def verify(args):
    config_values = candidate_configuration(args.candidate_env, args.active_model_env)
    checks = []

    def check(name, passed):
        if not passed:
            raise AssertionError(name)
        checks.append(name)
        print("PASS " + name, flush=True)

    async with ChatCompletionsClient(api_key=config_values["LLM_API_KEY"],
                           base_url=config_values["LLM_BASE_URL"],
                           reasoning_effort=reasoning_settings()['openai_reasoning_effort'],
                           timeout=30, max_retries=0) as client:
        await client.models.retrieve(config_values["LLM_MODEL"])
        response = await client.chat.completions.create(
            model=config_values["LLM_MODEL"],
            messages=[{"role": "user", "content": "Call connection_check with ok=true."}],
            tools=[{"type": "function", "function": {"name": "connection_check",
                "description": "Connectivity check; no business action.", "strict": True,
                "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"], "additionalProperties": False}}}],
            tool_choice="required", max_completion_tokens=64, temperature=0)
        calls = response.choices[0].message.tool_calls or []
        check("OpenAI connection, selected model and strict tool calling",
              bool(calls) and calls[0].function.name == "connection_check" and
              json.loads(calls[0].function.arguments).get("ok") is True)
        response = await client.chat.completions.create(model=config_values["LLM_MODEL"],
            messages=[{"role": "user", "content": 'Return JSON {"ok":true}.'}],
            response_format={"type": "json_object"}, max_completion_tokens=64, temperature=0)
        check("Semantic extraction JSON output", json.loads(response.choices[0].message.content)["ok"] is True)
        response = await client.chat.completions.create(model=config_values["LLM_MODEL"],
            messages=[{"role": "user", "content": "Reply True."}],
            max_tokens=1, temperature=0, logprobs=True, top_logprobs=2,
            logit_bias={'6432': 1, '7983': 1})
        check("Semantic reranking log probabilities", bool(response.choices[0].logprobs.content))
    if args.connection_only:
        return {"checks": checks, "model": config_values["LLM_MODEL"]}

    # Deliberately fixed test ports: never load production database settings.
    config = Config(uri="bolt://127.0.0.1:17694", pending_uri="bolt://127.0.0.1:17696",
                    dataset="openai-acceptance-" + uuid4().hex)
    store = Store(config)
    pipeline = Pipeline(config)
    gateway = MobileGateway(store, pipeline, workspace_id=config.dataset)
    conversation_id = ""
    actor = "openai-acceptance-user"
    context = {"tenant": config.dataset, "dataset": config.dataset, "actor": actor,
               "permissions": ["read", "propose", "record_pending", "confirm"]}

    async def dispatch(operation, **payload):
        return await gateway.dispatch({"context": context, "operation": operation,
            "payload": {"language": "en", "conversation_id": conversation_id, **payload}})

    async def poll(request_id):
        for _ in range(180):
            result = await dispatch("request_status", request_id=request_id)
            if result["status"] == "succeeded":
                return result["result"]
            if result["status"] in {"failed", "interrupted"}:
                raise AssertionError("Mobile request failed: " + result["error"]["code"])
            await asyncio.sleep(2)
        raise AssertionError("Mobile request did not finish within acceptance deadline")

    try:
        store.initialize()
        MobileAuth(store).initialize()
        store.write(lambda tx: tx.run(
            "CREATE (:KDWorkspace {id:$d,dataset:$d,status:'active'}) "
            "CREATE (:KDWorkspaceMember {id:$m,dataset:$d,workspace_id:$d,actor:$a,"
            "role:'owner',enabled:true})", d=config.dataset, a=actor,
            m=config.dataset + "-member").consume())
        conversation = await dispatch("conversation_create")
        conversation_id = conversation["id"]
        request_id = uuid4().hex
        await dispatch("chat_submit", request_id=request_id, message=MESSAGE)
        result = await poll(request_id)
        check("Mobile request completed and saved as pending", result.get("saved_pending"))
        rows = store.objects(limit=100)["rows"]
        check("All five clients persisted", all(
            any(row["name"] == name for row in rows) for name, *_ in CLIENTS))
        for name, city, person, industry in CLIENTS:
            client = next(row for row in rows if row["name"] == name)
            values = [str(f["value"].get("value", "")) for f in store.detail(client["id"])["rows"]]
            check("Client facts preserved: " + name, city in values and industry in values and (
                person in values or any(row["name"] == person for row in rows)))
        check("Records await explicit confirmation", all(not row["confirmed"] for row in rows))
        count = len(rows)
        await dispatch("chat_submit", request_id=request_id, message=MESSAGE)
        await poll(request_id)
        check("Same request retry creates no duplicate objects", len(store.objects(limit=100)["rows"]) == count)
        messages = await dispatch("messages")
        check("Original input preserved exactly once", sum(
            m["role"] == "user" and m["text"] == MESSAGE for m in messages["rows"]) == 1)
        visible = await dispatch("conversation_objects", limit=100)
        check("Conversation objects are visible", visible["total"] == count)
        batches = await dispatch("batches", limit=100)
        check("Pending review batch is available", bool(batches["rows"]))
        for batch in batches["rows"]:
            review = await dispatch("batch_review", batch_id=batch["id"],
                item_ids=[i["id"] for i in batch["items"] if i["status"] == "pending"], action="confirm")
            await dispatch("batch_apply", token=review["token"])
            repeated = await dispatch("batch_apply", token=review["token"])
            check("Confirmation retry is idempotent", repeated["idempotent"])
        check("Explicitly reviewed records are confirmed", all(
            row["confirmed"] for row in store.objects(limit=100)["rows"]))
        return {"checks": checks, "model": config_values["LLM_MODEL"],
                "dataset": config.dataset, "objects": count,
                "scope": "real model and mobile backend; excludes browser/Auth0 and semantic indexing"}
    finally:
        await gateway.close()
        await pipeline.close()
        store.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-env", required=True)
    parser.add_argument("--active-model-env")
    parser.add_argument("--connection-only", action="store_true")
    parser.add_argument("--report", default="artifacts/openai-acceptance.json")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        report = await verify(args)
        report.update(passed=True, elapsed_seconds=round(time.monotonic() - started, 2))
    except Exception as error:
        log_failure("openai_acceptance", error, elapsed=time.monotonic() - started)
        print("FAIL " + type(error).__name__, file=sys.stderr)
        return 1
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
