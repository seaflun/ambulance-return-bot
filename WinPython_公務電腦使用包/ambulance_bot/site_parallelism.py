from __future__ import annotations

import os


DEFAULT_PARALLEL_SITE_GROUPS = 4


def parallel_site_groups() -> int:
    value = os.getenv("WORKER_PARALLEL_SITE_GROUPS", "").strip()
    if not value:
        return DEFAULT_PARALLEL_SITE_GROUPS
    return 4 if value == "4" else 2
