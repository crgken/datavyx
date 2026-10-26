"""Real-model phrasing checks using explicit query-result fixtures, not business writes.

Inspect the saved answers for semantic accuracy. Application boundary assertions
live in verify_guards; end-to-end real query calls live in acceptance.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from demo.config import load_model_env
from demo.agent_runtime import AgentRuntime, INSTRUCTIONS
from pydantic_ai import Agent
from demo.model import QueryAnswer
from demo.service import Service


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-env", required=True)
    args = parser.parse_args()
    load_model_env(args.model_env)
    load_dotenv(ROOT / ".env")
    runtime = AgentRuntime(None, None)
    agent = Agent(runtime.agent.model, output_type=QueryAnswer, instructions=INSTRUCTIONS.split("【查询和回答】")[1].split("【确认执行阶段】")[0] + "本次是回答表达夹具验证，query_result 已由受控程序提供，无需调用工具。仅根据该结果返回 answer。", model_settings={"temperature":0,"timeout":90}, retries=2)
    base = {"scope": "数据库中已确认的当前有效记录，不保证现实业务全部录入",
            "revision": 1, "population_count": 10, "matched_count": 10,
            "participating_count": 10, "excluded_count": 0, "exclusion_summary": {},
            "offset": 0, "limit": 2, "has_more": False, "complete": True}
    cases = [
        ("sum_exclusions", "合同金额总共多少人民币？",
         {"operation": "SUM", "sum": "650000", "unit": "CNY", "total": 3,
          "participating_count": 3, "excluded_count": 7,
          "exclusion_summary": {"属性缺失或待确认": 7}}),
        ("empty_sum", "设备成本总额是多少人民币？",
         {"operation": "SUM", "sum": "0", "unit": "CNY", "total": 0,
          "population_count": 0, "matched_count": 0, "participating_count": 0}),
        ("partial_list", "列出全部合同。",
         {"operation": "LIST", "rows": [{"name": "合同 A"}, {"name": "合同 B"}],
          "total": 10, "returned_count": 2, "has_more": True, "complete": False}),
        ("partial_groups", "按客户统计合同数量。",
         {"operation": "GROUP", "groups": [{"value": {"value": "甲公司"}, "count": 6},
                                              {"value": {"value": "未确定"}, "count": 2}],
          "total": 3, "total_groups": 3, "returned_count": 2,
          "unknown_count": 2, "unknown_reasons": {"属性缺失或待确认": 2},
          "has_more": True, "complete": False}),
    ]
    report = {"fixture_answers": [], "boundary_checks": []}
    try:
        for name, question, fields in cases:
            result = {**base, **fields}
            run = await agent.run(json.dumps({"question":question,"query_result":result},ensure_ascii=False))
            answer = run.output.answer
            report["fixture_answers"].append({"name": name, "question": question,
                                                "result": result, "answer": answer})
            print(json.dumps({"name": name, "answer": answer}, ensure_ascii=False), flush=True)
    finally:
        await runtime.close()

    target = ROOT / "artifacts" / "query-answer-fixtures.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
