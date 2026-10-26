"""Explicitly load known sample records into the local Demo (not NLP extraction)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from demo.config import Config
from demo.store import Store
from fixtures import commit, contract_change


def main():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    store = Store(Config.from_env())
    try:
        store.initialize()
        for identifier, company, amount, status in [("A", "甲公司", "200000", "审核中"), ("B", "甲公司", "300000", "已完成"), ("C", "乙公司", "100000", "审核中")]:
            commit(store, *contract_change(identifier, company, amount, status))
        print("Loaded three explicitly confirmed structured fixtures. Expected: COUNT=3, SUM(CNY)=600000. Existing identical identities are reused.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
