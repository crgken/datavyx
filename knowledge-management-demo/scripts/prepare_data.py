"""Prepare a fixed, independently inspectable CSV oracle; never reads Demo code."""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    folder = ROOT / "fixtures"
    folder.mkdir(exist_ok=True)
    with (folder / "truth.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["identifier", "customer", "name", "amount", "unit", "status"])
        for index in range(1, 106):
            writer.writerow([f"F{index:03}", ["客户甲", "客户乙", "客户丙"][(index-1)%3],
                             "同名合同" if index%10==0 else f"合同 F{index:03}", index*100,
                             "CNY", "审核中" if index%2 else "已完成"])
    print("fixtures/truth.csv: 105 contracts; CNY 556500; 53 in review; 35 per customer.")


if __name__ == "__main__":
    main()
