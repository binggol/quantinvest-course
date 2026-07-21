"""Compare a completed Qlib audit with a completed vn.py validation run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backtest_engine.engine_compare import compare_results, sha256_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qlib", required=True)
    parser.add_argument("--vnpy", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--out", default="data/advisor_pro_engine_comparison.json")
    return parser.parse_args()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> None:
    args = parse_args()
    qlib_path = Path(args.qlib)
    vnpy_path = Path(args.vnpy)
    manifest = read_json(Path(args.bundle) / "manifest.json")
    provenance_entry = (manifest.get("files") or manifest.get("payloads") or {}).get("provenance", {})
    provenance_file = provenance_entry.get("path") or provenance_entry.get("file") or "provenance.json"
    provenance = read_json(Path(args.bundle) / provenance_file)
    expected_sha = (
        (provenance.get("source_audit") or {}).get("sha256")
        or provenance.get("source_audit_sha256")
    )
    comparison = compare_results(
        read_json(qlib_path),
        read_json(vnpy_path),
        qlib_result_sha256=sha256_file(qlib_path),
        expected_source_sha256=expected_sha,
    )
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(output.resolve()), **comparison["publication_gate"], "execution_reproduction_passed": comparison["execution_reproduction_passed"]}, ensure_ascii=False, indent=2))
    if not comparison["execution_reproduction_passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()

