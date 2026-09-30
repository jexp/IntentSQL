#!/usr/bin/env python3
from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).with_name("demo_orders.csv")
random.seed(17)

countries = [
    ("France", "Europe"),
    ("Germany", "Europe"),
    ("Morocco", "Africa"),
    ("Canada", "North America"),
]
products = [
    ("Atlas", "Enterprise", 420.0),
    ("Beacon", "Enterprise", 320.0),
    ("Cedar", "Consumer", 120.0),
    ("Delta", "Consumer", 90.0),
]
customers = [f"cust_{i:02d}" for i in range(1, 21)]

rows = []
start = date(2026, 1, 1)
order_id = 1

for day_offset in range(90):
    d = start + timedelta(days=day_offset)
    # 1 or 2 events per day, deterministic but varied.
    events_today = 1 + (1 if day_offset % 3 == 0 else 0)

    for j in range(events_today):
        country, region = countries[(day_offset + j * 2) % len(countries)]
        product, category, base_amount = products[(day_offset * 2 + j) % len(products)]
        customer = customers[(day_offset * 3 + j * 7) % len(customers)]

        # Make France the clear total-revenue leader.
        country_multiplier = {
            "France": 1.55,
            "Germany": 1.20,
            "Morocco": 0.80,
            "Canada": 1.00,
        }[country]

        amount = round(
            base_amount * country_multiplier + (day_offset % 11) * 7.5 + j * 13.0,
            2,
        )

        # March has the highest average latency; failed requests are slower.
        month_bonus = {1: 20, 2: 55, 3: 115}[d.month]
        status = "failed" if (day_offset + j) % 7 in {0, 1} else "success"
        status_bonus = 80 if status == "failed" else 0
        latency_ms = 90 + month_bonus + status_bonus + ((day_offset * 13 + j * 17) % 70)

        rows.append(
            {
                "order_id": order_id,
                "date": d.isoformat(),
                "customer": customer,
                "country": country,
                "region": region,
                "product": product,
                "category": category,
                "amount": amount,
                "status": status,
                "latency_ms": latency_ms,
            }
        )
        order_id += 1

with OUT.open("w", encoding="utf-8", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

print(f"wrote {len(rows)} rows to {OUT}")
