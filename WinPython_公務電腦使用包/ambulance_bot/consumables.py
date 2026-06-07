from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


INVENTORY_PATH = Path(__file__).with_name("consumable_inventory.json")


@lru_cache(maxsize=1)
def consumable_inventory_options() -> list[dict[str, Any]]:
    if not INVENTORY_PATH.exists():
        return []
    raw_items = json.loads(INVENTORY_PATH.read_text(encoding="utf-8-sig"))
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_items:
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        options.append(
            {
                "name": name,
                "series": str(item.get("series") or "").strip(),
                "category": str(item.get("category") or "").strip(),
                "code": str(item.get("code") or "").strip(),
                "stock": int(item.get("stock") or 0),
                "source": str(item.get("source") or "").strip(),
            }
        )
    return sorted(options, key=lambda row: (row["category"], row["series"], row["name"]))
