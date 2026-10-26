"""Structured fixtures only. These do not claim to validate natural language extraction."""
import csv
from pathlib import Path


def contract_change(identifier, customer, amount, status="审核中", name=None, uncertain=False, unit="CNY"):
    name = name or "合同 " + identifier
    message = f"{customer}有{name}（编号{identifier}），金额{amount} {unit}，状态{status}。"
    objects = [{"ref": "customer", "name": customer, "kind": "客户", "identifier": customer},
               {"ref": "contract", "name": name, "kind": "合同", "identifier": identifier, "namespace": customer}]
    facts = [{"subject": "contract", "predicate": "客户", "object": "customer", "evidence": message},
             {"subject": "contract", "predicate": "状态", "value": {"type": "text", "value": status}, "evidence": message},
             {"subject": "contract", "predicate": "金额", "value": {"type": "text" if uncertain else "number", "value": str(amount), "unit": "" if uncertain else unit},
              "certain": not uncertain, "reason": "金额为估计值" if uncertain else "", "evidence": message}]
    return message, {"objects": objects, "facts": facts}


def commit(store, message, change):
    mid = store.message(message)
    draft = store.prepare(change, mid)
    store.commit(draft["draft_id"])
    return draft


def truth_rows():
    with (Path(__file__).resolve().parent.parent / "fixtures" / "truth.csv").open(encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def seed_truth(store):
    for row in truth_rows():
        commit(store, *contract_change(row["identifier"], row["customer"], row["amount"], row["status"], row["name"]))
