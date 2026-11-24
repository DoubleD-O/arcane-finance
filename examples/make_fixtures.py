"""Writes the synthetic October 2026 fixture files. Rerun to regenerate them."""

import csv
from pathlib import Path

from openpyxl import Workbook

OUT = Path(__file__).parent / "2026-10"

SG_HEADER = ["Period", "GL Code", "Description", "Amount (SGD)"]
SG_ACTUAL = [
    ("4000", "Product revenue", "-800000.00"),
    ("4010", "Services revenue", "-200000.00"),
    ("5000", "Cost of sales", "450000.00"),
    ("6000", "Salaries and wages", "250000.00"),
    ("6100", "Office rent", "60000.00"),
    ("6200", "Marketing", "40000.00"),
    ("6900", "Other expenses", "30000.00"),
    ("7000", "Depreciation", "20000.00"),
]
SG_BUDGET = [
    ("4000", "Product revenue", "-760000.00"),
    ("4010", "Services revenue", "-190000.00"),
    ("5000", "Cost of sales", "430000.00"),
    ("6000", "Salaries and wages", "260000.00"),
    ("6100", "Office rent", "60000.00"),
    ("6200", "Marketing", "50000.00"),
    ("6900", "Other expenses", "25000.00"),
    ("7000", "Depreciation", "20000.00"),
]

AU_HEADER = ["Account", "Account Name", "Amount AUD"]
AU_ACTUAL = [
    ("41000", "Sales", 500000),
    ("50000", "Cost of sales", 260000),
    ("61000", "Salaries", 120000),
    ("62000", "Occupancy", 30000),
    ("63000", "Advertising", 20000),
    ("69000", "Sundry expenses", 10000),
    ("71000", "Depreciation", 8000),
]
AU_BUDGET = [
    ("41000", "Sales", 520000),
    ("50000", "Cost of sales", 265000),
    ("61000", "Salaries", 115000),
    ("62000", "Occupancy", 30000),
    ("63000", "Advertising", 25000),
    ("69000", "Sundry expenses", 10000),
    ("71000", "Depreciation", 8000),
]


def write_sg(name, rows, period="2026-10"):
    with open(OUT / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(SG_HEADER)
        for account, desc, amount in rows:
            w.writerow([period, account, desc, amount])


def write_au(name, rows, header=AU_HEADER, title="Profit and loss, October 2026"):
    wb = Workbook()
    ws = wb.active
    ws.title = "P&L"
    ws.append(["Synthetic AU entity"])
    ws.append([title])
    ws.append(header)
    for row in rows:
        ws.append(list(row))
    notes = wb.create_sheet("Notes")
    notes.append(["Synthetic fixture. Amounts in AUD, shown positive."])
    wb.save(OUT / name)


def main():
    OUT.mkdir(exist_ok=True)
    write_sg("sg_actual_v1.csv", SG_ACTUAL)
    write_sg("sg_budget.csv", SG_BUDGET)
    write_sg("sg_actual_v2.csv", [r if r[0] != "6200" else ("6200", "Marketing", "41000.00") for r in SG_ACTUAL])
    write_sg("sg_actual_wrong_period.csv", SG_ACTUAL, period="2026-09")
    write_sg("sg_actual_duplicate.csv", SG_ACTUAL + [("6200", "Marketing", "40000.00")])
    write_au("au_budget.xlsx", AU_BUDGET)
    write_au("au_actual_v1.xlsx", AU_ACTUAL)
    write_au("au_actual_v2.xlsx", AU_ACTUAL[:5] + [("64000", "Consulting fees", 15000)] + AU_ACTUAL[5:])
    write_au("au_actual_drift.xlsx", AU_ACTUAL, header=["Account", "Account Name", "Amount (AUD)"])


if __name__ == "__main__":
    main()
