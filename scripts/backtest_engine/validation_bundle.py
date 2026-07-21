"""Immutable input bundle contract for independent backtest validators.

The bundle deliberately contains targets and raw quote inputs, not trades or
NAV produced by the primary engine.  Payload files are content-addressed by a
canonical manifest so a second engine can reject incomplete or modified input.
"""

from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


BUNDLE_SCHEMA = "quantinvest.validation_bundle"
BUNDLE_VERSION = 1

REQUIRED_QUOTE_COLUMNS = (
    "date",
    "instrument",
    "open",
    "high",
    "low",
    "close",
    "change",
    "volume_lots",
    "adj",
    "max_adj",
)
OPTIONAL_QUOTE_COLUMNS = (
    "suspended",
    "is_st",
    "has_price_limit",
    "limit_buy",
    "limit_sell",
    "limit_pct",
    "rule_source",
)

_PAYLOAD_PATHS = {
    "config": "config.json",
    "provenance": "provenance.json",
    "quotes": "quotes.csv.gz",
    "targets": "targets.json",
}
_INSTRUMENT_RE = re.compile(r"^(?:SH|SZ|BJ)\d{6}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WEIGHT_TOLERANCE = 1e-12


@dataclass(frozen=True)
class ValidationBundle:
    """Verified bundle payload returned by :func:`load_bundle`."""

    manifest: dict[str, Any]
    targets: dict[str, dict[str, float]]
    quotes: tuple[dict[str, Any], ...]
    config: dict[str, Any]
    provenance: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest": copy.deepcopy(self.manifest),
            "targets": copy.deepcopy(self.targets),
            "quotes": [copy.deepcopy(row) for row in self.quotes],
            "config": copy.deepcopy(self.config),
            "provenance": copy.deepcopy(self.provenance),
        }


def _json_error(message: str) -> ValueError:
    return ValueError(f"invalid validation bundle JSON: {message}")


def _validate_json_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _json_error(f"{path} must be finite")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _json_error(f"{path} contains a non-string object key")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise _json_error(f"{path} has unsupported type {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON deterministically and reject non-JSON or non-finite values."""

    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reject_constant(value: str) -> None:
    raise _json_error(f"non-finite number {value!r}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _json_error(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _load_canonical_json_bytes(payload: bytes, label: str) -> Any:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _json_error(f"{label} is not UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise _json_error(f"{label}: {exc.msg}") from exc
    if canonical_json_bytes(value) != payload:
        raise _json_error(f"{label} is not canonical JSON")
    return value


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return value


def _normalize_date(value: Any, label: str) -> str:
    if isinstance(value, datetime):
        if value.time() != time.min or value.tzinfo is not None:
            raise ValueError(f"{label} must not include a time or timezone")
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must use YYYY-MM-DD")
    return value


def _normalize_instrument(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _INSTRUMENT_RE.fullmatch(value):
        raise ValueError(f"{label} must use SH/SZ/BJ plus six digits")
    return value


def _finite_number(value: Any, label: str, *, nullable: bool) -> float | None:
    if _is_nullish(value):
        if nullable:
            return None
        if value is None or isinstance(value, str):
            raise ValueError(f"{label} is required")
        raise ValueError(f"{label} must be finite")
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if math.isnan(number) and nullable:
        return None
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _is_nullish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    try:
        return bool(value != value)
    except (TypeError, ValueError):
        return type(value).__name__ in {"NAType", "NaTType"}


def _normalize_targets(targets: Mapping[Any, Any]) -> dict[str, dict[str, float]]:
    targets = _as_mapping(targets, "targets")
    if not targets:
        raise ValueError("targets must contain at least one date")

    normalized: dict[str, dict[str, float]] = {}
    for raw_date, raw_weights in targets.items():
        target_date = _normalize_date(raw_date, "target date")
        if target_date in normalized:
            raise ValueError(f"duplicate target date after normalization: {target_date}")
        weights = _as_mapping(raw_weights, f"targets[{target_date}]")
        normalized_weights: dict[str, float] = {}
        total = 0.0
        for raw_instrument, raw_weight in weights.items():
            instrument = _normalize_instrument(
                raw_instrument, f"targets[{target_date}] instrument"
            )
            if instrument in normalized_weights:
                raise ValueError(
                    f"duplicate instrument {instrument} for target date {target_date}"
                )
            weight = _finite_number(
                raw_weight, f"targets[{target_date}][{instrument}]", nullable=False
            )
            assert weight is not None
            if weight < 0 or weight > 1:
                raise ValueError(
                    f"target weight for {instrument} on {target_date} must be within [0, 1]"
                )
            normalized_weights[instrument] = weight
            total += weight
        if total > 1 + _WEIGHT_TOLERANCE:
            raise ValueError(f"target weights on {target_date} exceed 1")
        normalized[target_date] = dict(sorted(normalized_weights.items()))
    return dict(sorted(normalized.items()))


def _target_payload(targets: Mapping[Any, Any]) -> dict[str, Any]:
    normalized = _normalize_targets(targets)
    return {
        "schema_version": BUNDLE_VERSION,
        "targets": [
            {"date": target_date, "weights": weights}
            for target_date, weights in normalized.items()
        ],
    }


def _parse_target_payload(value: Any) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "targets"}:
        raise ValueError("targets.json has an invalid schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != BUNDLE_VERSION:
        raise ValueError("targets.json has an unsupported schema version")
    rows = value["targets"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("targets.json must contain at least one target date")

    targets: dict[str, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"date", "weights"}:
            raise ValueError("targets.json target rows have an invalid schema")
        target_date = _normalize_date(row["date"], "target date")
        if target_date in targets:
            raise ValueError(f"duplicate target date: {target_date}")
        targets[target_date] = dict(_as_mapping(row["weights"], "target weights"))
    normalized = _normalize_targets(targets)
    if list(normalized) != [row["date"] for row in rows]:
        raise ValueError("targets.json dates are not sorted")
    return normalized


def _quote_records(quotes: Any) -> list[Mapping[str, Any]]:
    if hasattr(quotes, "to_dict") and not isinstance(quotes, Mapping):
        try:
            quotes = quotes.to_dict(orient="records")
        except TypeError:
            pass
    if isinstance(quotes, (str, bytes, Mapping)) or not isinstance(quotes, Iterable):
        raise ValueError("quotes must be an iterable of row mappings")
    rows = list(quotes)
    if not rows:
        raise ValueError("quotes must contain at least one row")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("each quote row must be a mapping")
    return rows


def _optional_quote_columns(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    allowed = set(REQUIRED_QUOTE_COLUMNS) | set(OPTIONAL_QUOTE_COLUMNS)
    present: set[str] = set()
    for index, row in enumerate(rows):
        unknown = set(row) - allowed
        if unknown:
            raise ValueError(f"quote row {index} has unknown columns: {sorted(unknown)}")
        missing = set(REQUIRED_QUOTE_COLUMNS) - set(row)
        if missing:
            raise ValueError(f"quote row {index} is missing columns: {sorted(missing)}")
        present.update(set(row) & set(OPTIONAL_QUOTE_COLUMNS))
    return tuple(column for column in OPTIONAL_QUOTE_COLUMNS if column in present)


def _nullable_bool(value: Any, label: str) -> bool | None:
    if _is_nullish(value):
        return None
    if type(value) is bool:
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{label} must be true, false, or empty")


def _normalize_quote_rows(
    rows: Sequence[Mapping[str, Any]], columns: Sequence[str]
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    numeric = set(REQUIRED_QUOTE_COLUMNS[2:]) | {"limit_pct"}
    booleans = {"suspended", "is_st", "has_price_limit", "limit_buy", "limit_sell"}

    for index, row in enumerate(rows):
        normalized_row: dict[str, Any] = {
            "date": _normalize_date(row.get("date"), f"quotes[{index}].date"),
            "instrument": _normalize_instrument(
                row.get("instrument"), f"quotes[{index}].instrument"
            ),
        }
        key = (normalized_row["date"], normalized_row["instrument"])
        if key in seen:
            raise ValueError(
                f"duplicate quote row for {normalized_row['instrument']} on {normalized_row['date']}"
            )
        seen.add(key)

        for column in columns[2:]:
            value = row.get(column)
            label = f"quotes[{index}].{column}"
            if column in numeric:
                number = _finite_number(value, label, nullable=True)
                if number is not None and column == "volume_lots" and number < 0:
                    raise ValueError(f"{label} must be non-negative")
                if number is not None and column in {"adj", "max_adj"} and number <= 0:
                    raise ValueError(f"{label} must be positive")
                if number is not None and column == "limit_pct" and not 0 <= number <= 1:
                    raise ValueError(f"{label} must be within [0, 1]")
                normalized_row[column] = number
            elif column in booleans:
                normalized_row[column] = _nullable_bool(value, label)
            elif column == "rule_source":
                if _is_nullish(value):
                    normalized_row[column] = None
                elif not isinstance(value, str) or value != value.strip() or "\x00" in value:
                    raise ValueError(f"{label} must be a trimmed string")
                else:
                    normalized_row[column] = value
            else:  # pragma: no cover - guarded by the column schema
                raise ValueError(f"unsupported quote column: {column}")
        normalized.append(normalized_row)
    return sorted(normalized, key=lambda row: (row["date"], row["instrument"]))


def _format_csv_value(value: Any) -> str:
    if value is None:
        return ""
    if type(value) is bool:
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _csv_bytes(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=list(columns),
        extrasaction="raise",
        lineterminator="\n",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _format_csv_value(row.get(column)) for column in columns})
    return stream.getvalue().encode("utf-8")


def _gzip_bytes(payload: bytes) -> bytes:
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as handle:
        handle.write(payload)
    return stream.getvalue()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _descriptor(path: str, payload: bytes) -> dict[str, Any]:
    return {"path": path, "sha256": sha256_bytes(payload), "size": len(payload)}


def build_bundle(
    output_dir: str | Path,
    targets: Mapping[Any, Any],
    quotes: Any,
    config: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a deterministic validation bundle and return its manifest.

    The manifest is written last and intentionally does not list or hash
    ``manifest.json`` itself.  Callers can persist ``sha256_bytes(
    canonical_json_bytes(manifest))`` as an external bundle identity.
    """

    normalized_config = dict(_as_mapping(config, "config"))
    normalized_provenance = dict(_as_mapping(provenance, "provenance"))
    _validate_json_value(normalized_config, "$.config")
    _validate_json_value(normalized_provenance, "$.provenance")

    target_document = _target_payload(targets)
    source_rows = _quote_records(quotes)
    optional_columns = _optional_quote_columns(source_rows)
    columns = REQUIRED_QUOTE_COLUMNS + optional_columns
    normalized_quotes = _normalize_quote_rows(source_rows, columns)

    payloads = {
        "config": canonical_json_bytes(normalized_config),
        "provenance": canonical_json_bytes(normalized_provenance),
        "quotes": _gzip_bytes(_csv_bytes(normalized_quotes, columns)),
        "targets": canonical_json_bytes(target_document),
    }
    manifest = {
        "files": {
            name: _descriptor(_PAYLOAD_PATHS[name], payloads[name])
            for name in sorted(_PAYLOAD_PATHS)
        },
        "quotes": {"columns": list(columns), "format": "csv.gz", "rows": len(normalized_quotes)},
        "schema": BUNDLE_SCHEMA,
        "targets": {"dates": len(target_document["targets"])},
        "version": BUNDLE_VERSION,
    }

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValueError("output_dir must be a directory")
    for name, payload in payloads.items():
        _atomic_write(root / _PAYLOAD_PATHS[name], payload)
    _atomic_write(root / "manifest.json", canonical_json_bytes(manifest))
    return copy.deepcopy(manifest)


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "files",
        "quotes",
        "schema",
        "targets",
        "version",
    }:
        raise ValueError("manifest.json has an invalid schema")
    if value["schema"] != BUNDLE_SCHEMA:
        raise ValueError("manifest.json has an unsupported bundle schema")
    if type(value["version"]) is not int or value["version"] != BUNDLE_VERSION:
        raise ValueError("manifest.json has an unsupported bundle version")

    files = value["files"]
    if not isinstance(files, dict) or set(files) != set(_PAYLOAD_PATHS):
        raise ValueError("manifest.json must list exactly the expected payload files")
    for name, expected_path in _PAYLOAD_PATHS.items():
        descriptor = files[name]
        if not isinstance(descriptor, dict) or set(descriptor) != {"path", "sha256", "size"}:
            raise ValueError(f"manifest descriptor for {name} has an invalid schema")
        if descriptor["path"] != expected_path:
            raise ValueError(f"manifest path for {name} is invalid")
        if not isinstance(descriptor["sha256"], str) or not _SHA256_RE.fullmatch(
            descriptor["sha256"]
        ):
            raise ValueError(f"manifest SHA256 for {name} is invalid")
        if type(descriptor["size"]) is not int or descriptor["size"] < 0:
            raise ValueError(f"manifest size for {name} is invalid")

    quote_info = value["quotes"]
    if not isinstance(quote_info, dict) or set(quote_info) != {"columns", "format", "rows"}:
        raise ValueError("manifest quote metadata has an invalid schema")
    if quote_info["format"] != "csv.gz":
        raise ValueError("manifest quote format must be csv.gz")
    columns = quote_info["columns"]
    if not isinstance(columns, list) or any(not isinstance(item, str) for item in columns):
        raise ValueError("manifest quote columns must be a list of strings")
    required_count = len(REQUIRED_QUOTE_COLUMNS)
    if tuple(columns[:required_count]) != REQUIRED_QUOTE_COLUMNS:
        raise ValueError("manifest is missing or reordering required quote columns")
    optional = tuple(columns[required_count:])
    expected_optional = tuple(column for column in OPTIONAL_QUOTE_COLUMNS if column in optional)
    if optional != expected_optional or len(optional) != len(set(optional)):
        raise ValueError("manifest optional quote columns are invalid or out of order")
    if type(quote_info["rows"]) is not int or quote_info["rows"] <= 0:
        raise ValueError("manifest quote row count must be positive")

    target_info = value["targets"]
    if not isinstance(target_info, dict) or set(target_info) != {"dates"}:
        raise ValueError("manifest target metadata has an invalid schema")
    if type(target_info["dates"]) is not int or target_info["dates"] <= 0:
        raise ValueError("manifest target date count must be positive")
    return value


def _verified_payload(root: Path, name: str, descriptor: Mapping[str, Any]) -> bytes:
    candidate = root / str(descriptor["path"])
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"bundle payload is missing: {descriptor['path']}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"bundle payload escapes its root: {descriptor['path']}") from exc
    if not resolved.is_file():
        raise ValueError(f"bundle payload is not a regular file: {descriptor['path']}")
    payload = resolved.read_bytes()
    if len(payload) != descriptor["size"]:
        raise ValueError(f"bundle payload size mismatch: {name}")
    if sha256_bytes(payload) != descriptor["sha256"]:
        raise ValueError(f"bundle payload SHA256 mismatch: {name}")
    return payload


def _parse_quotes(payload: bytes, columns: Sequence[str], expected_rows: int) -> list[dict[str, Any]]:
    try:
        raw_csv = gzip.decompress(payload)
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise ValueError("quotes.csv.gz is not a valid gzip file") from exc
    try:
        text = raw_csv.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("quotes.csv.gz does not contain UTF-8 CSV") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(columns):
        raise ValueError("quotes.csv.gz header does not match its manifest")
    source_rows = list(reader)
    if len(source_rows) != expected_rows:
        raise ValueError("quotes.csv.gz row count does not match its manifest")
    normalized = _normalize_quote_rows(source_rows, columns)
    if _csv_bytes(normalized, columns) != raw_csv:
        raise ValueError("quotes.csv.gz does not contain canonical CSV")
    return normalized


def load_bundle(bundle_dir: str | Path) -> ValidationBundle:
    """Load a bundle only after strict schema, path, size, and hash checks."""

    root = Path(bundle_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("bundle_dir must be a directory")
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("validation bundle is missing manifest.json")
    manifest = _validate_manifest(
        _load_canonical_json_bytes(manifest_path.read_bytes(), "manifest.json")
    )

    payloads = {
        name: _verified_payload(root, name, descriptor)
        for name, descriptor in manifest["files"].items()
    }
    config = _load_canonical_json_bytes(payloads["config"], "config.json")
    provenance = _load_canonical_json_bytes(payloads["provenance"], "provenance.json")
    if not isinstance(config, dict):
        raise ValueError("config.json must contain an object")
    if not isinstance(provenance, dict):
        raise ValueError("provenance.json must contain an object")
    targets = _parse_target_payload(
        _load_canonical_json_bytes(payloads["targets"], "targets.json")
    )
    if len(targets) != manifest["targets"]["dates"]:
        raise ValueError("target date count does not match its manifest")
    quotes = _parse_quotes(
        payloads["quotes"], manifest["quotes"]["columns"], manifest["quotes"]["rows"]
    )
    return ValidationBundle(
        manifest=copy.deepcopy(manifest),
        targets=targets,
        quotes=tuple(quotes),
        config=copy.deepcopy(config),
        provenance=copy.deepcopy(provenance),
    )


__all__ = [
    "BUNDLE_SCHEMA",
    "BUNDLE_VERSION",
    "OPTIONAL_QUOTE_COLUMNS",
    "REQUIRED_QUOTE_COLUMNS",
    "ValidationBundle",
    "build_bundle",
    "canonical_json_bytes",
    "load_bundle",
    "sha256_bytes",
    "sha256_file",
]
