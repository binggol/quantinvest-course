from __future__ import annotations

import gzip
import json
from datetime import date

import pandas as pd
import pytest

from scripts.backtest_engine.validation_bundle import (
    REQUIRED_QUOTE_COLUMNS,
    build_bundle,
    canonical_json_bytes,
    load_bundle,
    sha256_bytes,
)


def quote_row(**overrides):
    row = {
        "date": "2026-07-10",
        "instrument": "SH600000",
        "open": 10.0,
        "high": 10.2,
        "low": 9.8,
        "close": 10.1,
        "change": 0.01,
        "volume_lots": 1_000,
        "adj": 1.25,
        "max_adj": 1.5,
    }
    row.update(overrides)
    return row


def build_sample(tmp_path, **overrides):
    values = {
        "output_dir": tmp_path,
        "targets": {
            "2026-07-10": {"SH600000": 0.6, "SZ000001": 0.4},
            "2026-07-13": {},
        },
        "quotes": [
            quote_row(),
            quote_row(instrument="SH000300", close=10.05),
            quote_row(date="2026-07-13", open=None, high=None, low=None, close=None,
                      change=None, volume_lots=0, adj=None, max_adj=None, suspended=True,
                      rule_source="missing_quote"),
        ],
        "config": {"capital": 1_000_000, "execution": {"price": "open"}},
        "provenance": {"source": "qlib-raw-export", "revision": "abc123"},
    }
    values.update(overrides)
    return build_bundle(**values)


def rewrite_payload_and_manifest(bundle_dir, name, payload):
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    path = bundle_dir / manifest["files"][name]["path"]
    path.write_bytes(payload)
    manifest["files"][name]["size"] = len(payload)
    manifest["files"][name]["sha256"] = sha256_bytes(payload)
    manifest_path.write_bytes(canonical_json_bytes(manifest))


def test_build_and_load_round_trip_is_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    manifest = build_sample(first)
    second_manifest = build_sample(second)

    assert manifest == second_manifest
    assert "manifest" not in manifest["files"]
    assert set(manifest["files"]) == {"config", "provenance", "quotes", "targets"}
    for descriptor in manifest["files"].values():
        assert (first / descriptor["path"]).read_bytes() == (
            second / descriptor["path"]
        ).read_bytes()

    bundle = load_bundle(first)
    assert bundle.targets["2026-07-10"] == {"SH600000": 0.6, "SZ000001": 0.4}
    assert bundle.targets["2026-07-13"] == {}
    assert bundle.quotes[0]["instrument"] == "SH000300"
    suspended = next(row for row in bundle.quotes if row["date"] == "2026-07-13")
    assert suspended["open"] is None
    assert suspended["volume_lots"] == 0
    assert suspended["suspended"] is True
    assert bundle.as_dict()["config"]["execution"]["price"] == "open"


def test_manifest_declares_required_quote_columns_and_optional_rule_fields(tmp_path):
    manifest = build_sample(tmp_path)
    columns = manifest["quotes"]["columns"]
    assert columns[: len(REQUIRED_QUOTE_COLUMNS)] == list(REQUIRED_QUOTE_COLUMNS)
    assert columns[-2:] == ["suspended", "rule_source"]


def test_bundle_accepts_point_in_time_st_and_no_limit_flags(tmp_path):
    build_sample(
        tmp_path,
        quotes=[quote_row(is_st=True, has_price_limit=False, limit_pct=0.05)],
    )
    row = load_bundle(tmp_path).quotes[0]
    assert row["is_st"] is True
    assert row["has_price_limit"] is False


def test_build_accepts_dataframe_midnight_dates_and_nan_as_missing_quotes(tmp_path):
    quotes = pd.DataFrame(
        [quote_row(date=pd.Timestamp("2026-07-10"), open=float("nan"), adj=pd.NA)]
    )
    build_sample(tmp_path, quotes=quotes)

    row = load_bundle(tmp_path).quotes[0]
    assert row["date"] == "2026-07-10"
    assert row["open"] is None
    assert row["adj"] is None


def test_quote_hash_detects_tampering(tmp_path):
    manifest = build_sample(tmp_path)
    quote_path = tmp_path / manifest["files"]["quotes"]["path"]
    quote_path.write_bytes(quote_path.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="size mismatch: quotes"):
        load_bundle(tmp_path)


def test_manifest_rejects_path_traversal_before_opening_payload(tmp_path):
    manifest = build_sample(tmp_path)
    manifest["files"]["quotes"]["path"] = "../quotes.csv.gz"
    (tmp_path / "manifest.json").write_bytes(canonical_json_bytes(manifest))

    with pytest.raises(ValueError, match="path for quotes is invalid"):
        load_bundle(tmp_path)


def test_load_rejects_noncanonical_json_even_when_manifest_hash_matches(tmp_path):
    build_sample(tmp_path)
    noncanonical = b'{\n  "capital": 1000000, "execution": {"price": "open"}\n}'
    rewrite_payload_and_manifest(tmp_path, "config", noncanonical)

    with pytest.raises(ValueError, match="config.json is not canonical JSON"):
        load_bundle(tmp_path)


def test_load_rejects_duplicate_target_dates(tmp_path):
    build_sample(tmp_path)
    targets = {
        "schema_version": 1,
        "targets": [
            {"date": "2026-07-10", "weights": {"SH600000": 1.0}},
            {"date": "2026-07-10", "weights": {}},
        ],
    }
    rewrite_payload_and_manifest(tmp_path, "targets", canonical_json_bytes(targets))

    with pytest.raises(ValueError, match="duplicate target date"):
        load_bundle(tmp_path)


def test_build_rejects_duplicate_dates_after_normalization(tmp_path):
    targets = {
        date(2026, 7, 10): {"SH600000": 1.0},
        "2026-07-10": {},
    }
    with pytest.raises(ValueError, match="duplicate target date after normalization"):
        build_sample(tmp_path, targets=targets)


@pytest.mark.parametrize(
    ("weights", "message"),
    [
        ({"SH600000": 0.6, "SZ000001": 0.5}, "exceed 1"),
        ({"SH600000": -0.01}, "within \\[0, 1\\]"),
        ({"SH600000": float("nan")}, "must be finite"),
    ],
)
def test_build_rejects_invalid_target_weights(tmp_path, weights, message):
    with pytest.raises(ValueError, match=message):
        build_sample(tmp_path, targets={"2026-07-10": weights})


def test_build_rejects_duplicate_quotes_and_invalid_quote_values(tmp_path):
    duplicate = [quote_row(), quote_row()]
    with pytest.raises(ValueError, match="duplicate quote row"):
        build_sample(tmp_path, quotes=duplicate)

    with pytest.raises(ValueError, match="volume_lots must be non-negative"):
        build_sample(tmp_path, quotes=[quote_row(volume_lots=-1)])

    with pytest.raises(ValueError, match="adj must be positive"):
        build_sample(tmp_path, quotes=[quote_row(adj=0)])


def test_load_rejects_noncanonical_csv_even_when_hash_matches(tmp_path):
    manifest = build_sample(tmp_path)
    quote_path = tmp_path / manifest["files"]["quotes"]["path"]
    raw = gzip.decompress(quote_path.read_bytes()).replace(b"10.0,", b"10.00,", 1)
    rewrite_payload_and_manifest(tmp_path, "quotes", gzip.compress(raw, mtime=0))

    with pytest.raises(ValueError, match="canonical CSV"):
        load_bundle(tmp_path)
