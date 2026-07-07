from __future__ import annotations

import argparse
from pathlib import Path

from .selenium_local import query_duty_emergency_cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local duty case lookup once.")
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--lookup-range", default="24h")
    args = parser.parse_args(argv)

    try:
        result = query_duty_emergency_cases(Path(args.artifacts_dir), lookup_range=args.lookup_range)
    except Exception as exc:
        print(f"[case_lookup] child failed: {exc}", flush=True)
        return 1

    count = len(result.cases) if isinstance(result.cases, list) else 0
    print(
        f"[case_lookup] child result status={result.status} count={count} detail={result.detail}",
        flush=True,
    )
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
