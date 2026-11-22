"""Write samples/sample_bom.xlsx: a fake BOM sheet for trying out the BOM upload.

Deterministic: the random values come from a fixed seed, so every run produces
the same file. DH1300X2700 is intentionally not registered as a product, so
uploading this file should flag those rows as unmatched.

Usage:
    python samples/make_sample_bom.py
"""

import random
from pathlib import Path

from openpyxl import Workbook

SEED = 20260101
OUTPUT = Path(__file__).resolve().parent / "sample_bom.xlsx"

HEADER = ["Ass Mark", "Profile", "Length(mm)", "Q'ty", "FINISH", "LOCATION"]
PROFILES = ["DH600X2700", "DH800X2700", "DH1300X2700"]
LOCATIONS = ["X3-X4/Y3-Y4", "X4-X5/Y3-Y4", "X1-X2/Y1-Y2", "X5-X6/Y4-Y5"]
FINISH = "3TB0"


def build_rows():
    rng = random.Random(SEED)
    marks = [f"Z-3B{n}" for n in range(201, 216)] + [f"Z-3G{n}" for n in range(201, 216)]
    rows = []
    for i, mark in enumerate(marks):
        profile = PROFILES[i % len(PROFILES)]
        length = rng.randrange(600, 5601, 10)
        qty = rng.randint(1, 3)
        location = LOCATIONS[i % len(LOCATIONS)]
        rows.append([mark, profile, length, qty, FINISH, location])
    return rows


def main():
    wb = Workbook()
    ws = wb.active
    ws.title = "BOM"
    ws.append(HEADER)
    for row in build_rows():
        ws.append(row)
    wb.save(OUTPUT)
    print(f"wrote {OUTPUT} ({len(ws['A']) - 1} data rows)")


if __name__ == "__main__":
    main()
