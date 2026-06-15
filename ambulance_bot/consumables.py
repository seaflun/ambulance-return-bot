from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


INVENTORY_PATH = Path(__file__).with_name("consumable_inventory.json")
COMMON_CONSUMABLES: list[tuple[str, str]] = [
    ("其它類", "桃-血糖試紙(片)"),
    ("其它類", "桃-安全型採血針(支)"),
    ("其它類", "桃-可拋棄式耳溫槍耳套-福爾TD-1118(個)"),
    ("其它類", "桃-心電圖電極貼片(片)"),
    ("其它類", "桃-拋棄式CPR回饋貼片(組)"),
    ("呼吸治療類", "桃-鼻管(條)"),
    ("呼吸治療類", "桃-成人氧氣面罩(個)"),
    ("呼吸治療類", "桃-成人非再呼吸型面罩(組)"),
    ("呼吸治療類", "桃-成人甦醒球(組)"),
    ("呼吸治療類", "桃-連接管-長管(條)"),
    ("呼吸道類", "桃-非充氣聲門上呼吸道-3號(組)"),
    ("呼吸道類", "桃-非充氣聲門上呼吸道-4號(組)"),
    ("呼吸道類", "桃-非充氣聲門上呼吸道-5號(組)"),
    ("呼吸道類", "桃-細菌過濾器(組)"),
    ("注射類", "桃-酒精棉片(片)"),
    ("注射類", "桃-18號防回血IC針(支)"),
    ("注射類", "桃-20號防回血IC針(支)"),
    ("注射類", "桃-22號防回血IC針(支)"),
    ("注射類", "桃-24號防回血IC針(支)"),
    ("注射類", "桃-免針型輸液套(組)"),
    ("注射類", "桃-透明敷料op site(片)"),
    ("注射類", "桃-15mm拋棄式骨內血管穿刺針具(組)"),
    ("注射類", "桃-25mm拋棄式骨內血管穿刺針具(組)"),
    ("注射類", "桃-45mm拋棄式骨內血管穿刺針具(組)"),
    ("注射類", "桃-10ml預充式導管沖洗器(支)"),
]
COMMON_CONSUMABLE_RANK = {name: index for index, (_category, name) in enumerate(COMMON_CONSUMABLES)}
COMMON_CATEGORY_RANK: dict[str, int] = {}
for category, _name in COMMON_CONSUMABLES:
    COMMON_CATEGORY_RANK.setdefault(category, len(COMMON_CATEGORY_RANK))


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
    for category, name in COMMON_CONSUMABLES:
        if name in seen:
            continue
        seen.add(name)
        options.append(
            {
                "name": name,
                "series": name.rsplit("(", 1)[0],
                "category": category,
                "code": "",
                "stock": 0,
                "source": "common",
            }
        )
    fallback_rank = len(COMMON_CONSUMABLE_RANK)
    fallback_category_rank = len(COMMON_CATEGORY_RANK)
    return sorted(
        options,
        key=lambda row: (
            COMMON_CONSUMABLE_RANK.get(row["name"], fallback_rank),
            COMMON_CATEGORY_RANK.get(row["category"], fallback_category_rank),
            row["category"],
            row["series"],
            row["name"],
        ),
    )
