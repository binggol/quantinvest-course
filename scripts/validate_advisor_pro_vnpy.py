"""Run the independent vn.py validator against a frozen input bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_engine.validation_bundle import load_bundle
from scripts.backtest_engine.vnpy_validator import QuantInvestPortfolioEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, help="Verified validation-bundle directory")
    parser.add_argument(
        "--out", default="data/advisor_pro_vnpy_validation.json", help="Result JSON path"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bundle = load_bundle(Path(args.bundle))
    result = QuantInvestPortfolioEngine(bundle).run_validation()
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    temporary.replace(output)
    print(
        json.dumps(
            {
                "out": str(output.resolve()),
                "days": len(result["daily_path"]),
                "attempts": result["orders"]["attempts"],
                "trades": result["orders"]["trades"],
                "final_account": result["execution_metrics"]["final_account"],
                "final_holding_count": result["final_position"]["holding_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
