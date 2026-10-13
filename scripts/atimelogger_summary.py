"""Write only a private aggregate; never print account data or raw errors."""
import argparse
from datetime import date
import json
from pathlib import Path
import sys

from timex.atimelogger import collect_summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, required=True,
                        help="Private JSON: category path or id:<ID> -> TimeX category")
    parser.add_argument("--timezone-fallback", help="Explicit IANA zone if account zone is unset")
    parser.add_argument("--period-end", type=date.fromisoformat, help="Last completed local day")
    parser.add_argument("--lookback-days", type=int, default=1,
                        help="Preceding days to query for carry-in records (1-93; default 1)")
    parser.add_argument("--output", type=Path, required=True, help="Private aggregate destination")
    args = parser.parse_args()
    if args.output.exists():
        print("Output exists; refusing overwrite.", file=sys.stderr)
        return 2
    try:
        mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
        if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                 for k, v in mapping.items()):
            raise ValueError("invalid mapping")
        result = collect_summary(mapping=mapping, timezone_fallback=args.timezone_fallback,
                                 period_end=args.period_end, lookback_days=args.lookback_days)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Aggregate and mapping are private too, even though raw records are absent.
        with args.output.open("x", encoding="utf-8") as destination:
            args.output.chmod(0o600)
            json.dump(result, destination, ensure_ascii=False, indent=2)
    except Exception:
        print("Summary failed; private data and error details suppressed.", file=sys.stderr)
        return 1
    print("Private aggregate saved. Raw records not persisted. Model calls: 0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
