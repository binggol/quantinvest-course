"""Factor-batch contract helpers shared by Qlib training entry points."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


DEFAULT_ALL_FEATURES = (
    "RSI_6D_Rank",
    "RSQR5",
    "VSTD5",
    "STD5",
    "CORD5",
    "CORD10",
    "WVMA60",
    "CORD60",
    "KLEN",
    "KLOW",
    "WVMA5",
    "CORR10",
    "RESI5",
    "CORR5",
    "RSQR20",
    "RESI10",
    "RSQR10",
    "BB_Width_20D_Rank",
    "ROC60",
    "CORR20",
    "RSQR60",
    "CORR60",
    "BB_Position_10D_Rank",
    "RSI_14D_Rank",
)


def _factor_names(values: Iterable[object], field: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field} must be a list of factor names")

    names: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} contains an invalid factor name: {value!r}")
        names.add(value.strip())
    return frozenset(names)


@dataclass(frozen=True)
class FactorContract:
    """Factors selected from the universe evaluated for a mining batch."""

    effective_factors: frozenset[str] | None
    all_features: frozenset[str]

    @property
    def excluded_features(self) -> frozenset[str]:
        if self.effective_factors is None:
            return frozenset()
        return self.all_features - self.effective_factors

    @property
    def enabled(self) -> bool:
        return self.effective_factors is not None

    @classmethod
    def disabled(cls, all_features: Iterable[object] = DEFAULT_ALL_FEATURES) -> "FactorContract":
        return cls(None, _factor_names(all_features, "all_features"))


def contract_from_manifest(
    manifest: Mapping[str, object],
    default_all_features: Iterable[object] = DEFAULT_ALL_FEATURES,
) -> FactorContract:
    """Parse the same effective/all-features contract used by prediction.

    A batch manifest without ``effective_factors`` intentionally resolves to an
    empty selection, matching ``predict_next_day.py``. This fails closed instead
    of silently training on every evaluated candidate.
    """

    effective = _factor_names(manifest.get("effective_factors", ()), "effective_factors")
    all_features = _factor_names(
        manifest.get("all_features", default_all_features),
        "all_features",
    )
    return FactorContract(effective, all_features)


def contract_from_effective_factors(
    effective_factors: Iterable[object] | None,
    all_features: Iterable[object] = DEFAULT_ALL_FEATURES,
) -> FactorContract:
    """Build the default-SOTA contract, where a missing selection disables filtering."""

    if effective_factors is None:
        return FactorContract.disabled(all_features)
    return FactorContract(
        _factor_names(effective_factors, "effective_factors"),
        _factor_names(all_features, "all_features"),
    )


def filter_feature_frame(frame, excluded_features: Iterable[str]):
    """Return a frame without excluded feature columns, preserving labels.

    Qlib handler frames normally use ``(group, name)`` MultiIndex columns. Flat
    columns are also supported for feature-only prepared frames.
    """

    import pandas as pd

    excluded = frozenset(excluded_features)
    if not isinstance(frame, pd.DataFrame) or not excluded:
        return frame

    if isinstance(frame.columns, pd.MultiIndex):
        keep = [
            column
            for column in frame.columns
            if not (
                len(column) >= 2
                and column[0] == "feature"
                and column[1] in excluded
            )
        ]
    else:
        keep = [column for column in frame.columns if column not in excluded]
    return frame.loc[:, keep]


def apply_contract_to_handler(
    handler,
    contract: FactorContract,
    frame_attributes: Sequence[str] = ("_infer", "_learn", "_data"),
) -> dict[str, int]:
    """Apply a factor contract to every materialized Qlib handler frame."""

    import pandas as pd

    removed: dict[str, int] = {}
    for attribute in frame_attributes:
        frame = getattr(handler, attribute, None)
        if not isinstance(frame, pd.DataFrame):
            continue
        filtered = filter_feature_frame(frame, contract.excluded_features)
        removed[attribute] = len(frame.columns) - len(filtered.columns)
        if filtered is not frame:
            setattr(handler, attribute, filtered)
    return removed
