"""Compatibility entry point for the replacement Pydantic AI conversation suite.

Runs real-model, real-Neo4j scenarios from verify_agent, including clarification,
query detours, restart recovery, source retention, topic changes and cancellation.
"""
from verify_agent import main

if __name__ == "__main__":
    main()