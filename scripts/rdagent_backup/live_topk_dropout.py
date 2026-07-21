"""Pure live-portfolio helpers matching Qlib's TopkDropout signal logic.

The backtest owns an evolving position, while ``predict_next_day.py`` used to
publish the latest raw top-k scores on every run.  That silently turned a
``TopkDropoutStrategy(topk=50, n_drop=5)`` backtest into a full-rebalance live
portfolio.  This module keeps the state transition independent from Qlib and
from file I/O so it can be tested directly and deployed beside the predictor.

The transition mirrors Qlib's default ``method_buy='top'`` and
``method_sell='bottom'`` decision *before* next-day tradability and fill checks.
Those checks cannot be known when publishing a next-trading-day target and are
therefore recorded separately in the output metadata by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
from typing import Any, Iterable, Mapping, Sequence


SUCCESS_STATES = {"ok", "success", "succeeded", "complete", "completed", "done", "published"}
FAILURE_STATES = {"error", "failed", "failure", "cancelled", "canceled", "partial", "running"}


def _code(value: Any) -> str:
    return "" if value is None else str(value).strip().upper()


def _normal_batch(value: Any) -> str:
    normalized = str(value or "").strip()
    return "default" if normalized.lower() in {"", "default"} else normalized


def _normal_mode(value: Any) -> str:
    # Legacy buylist_history rows were all factor-batch predictions.
    return str(value or "factor").strip().lower()


def _parse_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return datetime.min
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.min


def _unique_codes(values: Iterable[Any]) -> tuple[str, ...] | None:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("code")
        code = _code(value)
        if not code or code in seen:
            return None
        seen.add(code)
        result.append(code)
    return tuple(result)


def _history_codes(run: Mapping[str, Any]) -> tuple[tuple[str, ...] | None, str]:
    portfolio = run.get("portfolio")
    if (
        isinstance(portfolio, Mapping)
        and isinstance(portfolio.get("target"), Sequence)
        and not isinstance(portfolio.get("target"), (str, bytes))
    ):
        return _unique_codes(portfolio["target"]), "portfolio.target"
    hits = run.get("hits")
    if isinstance(hits, Sequence) and not isinstance(hits, (str, bytes)):
        return _unique_codes(hits), "legacy_hits"
    return None, "missing"


def _run_succeeded(run: Mapping[str, Any]) -> bool:
    if run.get("portfolio_state_valid") is False:
        return False
    raw = run.get("status", run.get("state"))
    if raw is None:
        # Historical rows predate explicit status.  Structural validation below
        # remains mandatory before they can bootstrap the first stateful run.
        return True
    state = str(raw).strip().lower()
    if state in FAILURE_STATES:
        return False
    return state in SUCCESS_STATES


def _strategy_matches(
    run: Mapping[str, Any],
    *,
    topk: int,
    n_drop: int,
    hold_thresh: int,
    only_tradable: bool,
) -> bool:
    strategy = run.get("strategy")
    if not isinstance(strategy, Mapping):
        return True  # migration path for structurally valid legacy rows
    name = str(strategy.get("name") or "TopkDropoutStrategy")
    try:
        row_topk = int(strategy.get("topk", run.get("top_k", topk)))
        row_drop = int(strategy.get("n_drop", n_drop))
        row_hold = int(strategy.get("hold_thresh", hold_thresh))
    except (TypeError, ValueError):
        return False
    return (
        name == "TopkDropoutStrategy"
        and row_topk == topk
        and row_drop == n_drop
        and row_hold == hold_thresh
        and bool(strategy.get("only_tradable", only_tradable)) is only_tradable
        and str(strategy.get("method_buy", "top")) == "top"
        and str(strategy.get("method_sell", "bottom")) == "bottom"
    )


@dataclass(frozen=True)
class PreviousHoldings:
    """The most recent validated portfolio snapshot for one exact sleeve."""

    codes: tuple[str, ...]
    as_of: str | None
    generated_at: str | None
    source_schema: str
    initialized: bool
    rejection_counts: Mapping[str, int]

    def metadata(self, current_as_of: str) -> dict[str, Any]:
        previous = _parse_date(self.as_of)
        current = _parse_date(current_as_of)
        gap = (current - previous).days if previous and current else None
        return {
            "initialized": self.initialized,
            "previous_as_of": self.as_of,
            "previous_generated_at": self.generated_at,
            "source_schema": self.source_schema,
            "calendar_gap_days": gap,
            "rejection_counts": dict(self.rejection_counts),
        }


def select_previous_holdings(
    history_runs: Sequence[Mapping[str, Any]],
    *,
    current_as_of: str,
    model: str,
    universe: str,
    batch: str,
    mode: str,
    topk: int,
    n_drop: int,
    current_universe_size: int,
    current_codes: Iterable[Any],
    hold_thresh: int = 1,
    only_tradable: bool = True,
    minimum_overlap: float = 0.80,
    minimum_universe_size_ratio: float = 0.80,
    maximum_calendar_gap_days: int = 14,
) -> PreviousHoldings:
    """Select the latest *strictly earlier* valid holding snapshot.

    Exact sleeve identity prevents, for example, a CSI 300 or Alpha158 list from
    becoming the state of a CSI 1000 factor-batch portfolio.  Universe-size and
    code-overlap checks also reject mislabeled snapshots created from an old or
    truncated constituent file.
    """

    if topk <= 0:
        raise ValueError("topk must be positive")
    if n_drop < 0 or n_drop > topk:
        raise ValueError("n_drop must be in [0, topk]")
    if current_universe_size <= 0:
        raise ValueError("current_universe_size must be positive")
    if not (0.0 <= minimum_overlap <= 1.0):
        raise ValueError("minimum_overlap must be in [0, 1]")
    if not (0.0 < minimum_universe_size_ratio <= 1.0):
        raise ValueError("minimum_universe_size_ratio must be in (0, 1]")
    if maximum_calendar_gap_days < 1:
        raise ValueError("maximum_calendar_gap_days must be positive")

    current_date = _parse_date(current_as_of)
    if current_date is None:
        raise ValueError(f"invalid current_as_of={current_as_of!r}")
    available = {_code(code) for code in current_codes if _code(code)}
    if len(available) != current_universe_size:
        raise ValueError(
            "current_universe_size does not match the unique current score universe: "
            f"{current_universe_size} != {len(available)}"
        )

    wanted_model = str(model).strip().lower()
    wanted_universe = str(universe).strip().lower()
    wanted_batch = _normal_batch(batch)
    wanted_mode = _normal_mode(mode)
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    ordered = sorted(
        (run for run in history_runs if isinstance(run, Mapping)),
        key=lambda run: (_parse_date(run.get("as_of")) or date.min, _parse_datetime(run.get("generated_at"))),
        reverse=True,
    )
    for run in ordered:
        if (
            str(run.get("model") or "").strip().lower() != wanted_model
            or str(run.get("universe") or "").strip().lower() != wanted_universe
            or _normal_batch(run.get("batch")) != wanted_batch
            or _normal_mode(run.get("mode")) != wanted_mode
        ):
            reject("identity_mismatch")
            continue
        row_date = _parse_date(run.get("as_of"))
        if row_date is None or row_date >= current_date:
            reject("not_strictly_previous")
            continue
        if (current_date - row_date).days > maximum_calendar_gap_days:
            reject("stale_snapshot")
            continue
        if not _run_succeeded(run):
            reject("unsuccessful")
            continue
        if not _strategy_matches(
            run,
            topk=topk,
            n_drop=n_drop,
            hold_thresh=hold_thresh,
            only_tradable=only_tradable,
        ):
            reject("strategy_mismatch")
            continue

        codes, source = _history_codes(run)
        if not codes or len(codes) > topk:
            reject("invalid_holdings")
            continue
        try:
            old_universe_size = int(run.get("n_universe"))
        except (TypeError, ValueError):
            reject("invalid_universe_size")
            continue
        expected_size = min(topk, old_universe_size)
        if old_universe_size <= 0 or len(codes) != expected_size:
            reject("incomplete_holdings")
            continue
        size_ratio = min(old_universe_size, current_universe_size) / max(
            old_universe_size, current_universe_size
        )
        if size_ratio < minimum_universe_size_ratio:
            reject("universe_size_mismatch")
            continue
        overlap = len(set(codes) & available) / len(codes)
        if overlap < minimum_overlap:
            reject("low_code_overlap")
            continue
        return PreviousHoldings(
            codes=codes,
            as_of=str(run.get("as_of"))[:10],
            generated_at=str(run.get("generated_at")) if run.get("generated_at") else None,
            source_schema=source,
            initialized=False,
            rejection_counts=rejected,
        )

    return PreviousHoldings(
        codes=(),
        as_of=None,
        generated_at=None,
        source_schema="none",
        initialized=True,
        rejection_counts=rejected,
    )


@dataclass(frozen=True)
class TopkDropoutTransition:
    retained: tuple[str, ...]
    sold: tuple[str, ...]
    added: tuple[str, ...]
    target: tuple[str, ...]
    signal_order: tuple[str, ...]
    initialized: bool

    def action_for(self, code: Any) -> str:
        return "added" if _code(code) in set(self.added) else "retained"


def capped_topk_dropout(
    previous_holdings: Iterable[Any],
    ranked_codes: Iterable[Any],
    *,
    topn: int,
    max_replacements: int,
) -> list[str]:
    """Apply the repository's tested deterministic Qlib-style replacement cap.

    This is the lightweight shared implementation used by both the Advisor Pro
    frequency backtest and RD-Agent's live portfolio transition.  Callers own
    market-specific code normalization before invoking it.
    """

    target_count = int(topn)
    replacement_limit = int(max_replacements)
    if target_count < 1:
        raise ValueError("topn must be positive")
    if not 0 <= replacement_limit <= target_count:
        raise ValueError("max_replacements must be between 0 and topn")

    ranking_raw = _unique_codes(ranked_codes)
    current_raw = _unique_codes(previous_holdings)
    if ranking_raw is None:
        raise ValueError("ranking must contain unique non-empty codes")
    if current_raw is None:
        raise ValueError("previous holdings must contain unique non-empty codes")
    ranking = list(ranking_raw)
    current = list(current_raw)
    if len(current) > target_count:
        raise ValueError("previous holdings exceed topn")
    if not current:
        if len(ranking) < target_count:
            raise ValueError("ranking has fewer codes than topn")
        return ranking[:target_count]

    rank_position = {code: index for index, code in enumerate(ranking)}

    def rank_key(code: str) -> tuple[float, str]:
        return float(rank_position.get(code, math.inf)), code

    missing_slots = target_count - len(current)
    current_set = set(current)
    candidates = [code for code in ranking if code not in current_set]
    today = candidates[: replacement_limit + missing_slots]
    combined = sorted(set(current) | set(today), key=rank_key)
    bottom = set(combined[-replacement_limit:]) if replacement_limit else set()
    sold = [code for code in current if code in bottom]
    buys = today[: len(sold) + missing_slots]
    result = sorted((set(current) - set(sold)) | set(buys), key=rank_key)
    if len(result) != target_count:
        raise ValueError("ranking cannot fill the requested topn portfolio")
    return result


def topk_dropout_transition(
    scores: Sequence[tuple[Any, Any]] | Mapping[Any, Any],
    previous_holdings: Sequence[Any],
    *,
    topk: int,
    n_drop: int,
) -> TopkDropoutTransition:
    """Apply Qlib's default TopkDropout ranking transition without file I/O.

    ``scores`` order is the deterministic tie-breaker.  Existing holdings that
    disappeared from today's score universe sort last, matching pandas/Qlib NaN
    ordering, and are retired gradually under the same ``n_drop`` limit.
    """

    if topk <= 0:
        raise ValueError("topk must be positive")
    if n_drop < 0 or n_drop > topk:
        raise ValueError("n_drop must be in [0, topk]")
    raw_items = list(scores.items()) if isinstance(scores, Mapping) else list(scores)
    normalized: list[tuple[str, float, int]] = []
    seen_scores: set[str] = set()
    for position, (raw_code, raw_score) in enumerate(raw_items):
        code = _code(raw_code)
        if not code or code in seen_scores:
            raise ValueError(f"duplicate or empty score code: {raw_code!r}")
        try:
            score = float(raw_score)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid score for {code}: {raw_score!r}") from exc
        if not math.isfinite(score):
            raise ValueError(f"non-finite score for {code}: {raw_score!r}")
        seen_scores.add(code)
        normalized.append((code, score, position))
    if not normalized:
        raise ValueError("scores must not be empty")

    previous = _unique_codes(previous_holdings)
    if previous is None:
        raise ValueError("previous_holdings must contain unique non-empty codes")
    if len(previous) > topk:
        raise ValueError("previous_holdings cannot exceed topk")

    # Python's stable sort preserves input order for equal scores, providing an
    # explicit deterministic equivalent to the score ordering used by Qlib.
    ranked = tuple(code for code, _score, _pos in sorted(normalized, key=lambda item: -item[1]))
    target = tuple(
        capped_topk_dropout(
            previous,
            ranked,
            topn=topk,
            max_replacements=n_drop,
        )
    )
    target_set = set(target)
    previous_set = set(previous)
    sold = tuple(code for code in previous if code not in target_set)
    added = tuple(code for code in target if code not in previous_set)
    retained = tuple(code for code in previous if code in target_set)

    return TopkDropoutTransition(
        retained=retained,
        sold=sold,
        added=added,
        target=target,
        signal_order=ranked,
        initialized=not bool(previous),
    )


def strategy_metadata(
    *,
    topk: int,
    n_drop: int,
    hold_thresh: int = 1,
    only_tradable: bool = True,
) -> dict[str, Any]:
    """Return the persisted strategy identity used by history validation."""

    return {
        "name": "TopkDropoutStrategy",
        "topk": int(topk),
        "n_drop": int(n_drop),
        "method_buy": "top",
        "method_sell": "bottom",
        "hold_thresh": int(hold_thresh),
        "only_tradable": bool(only_tradable),
        "decision_scope": "pre_execution_target",
        "future_tradability": "checked_at_next_day_execution_not_prediction_time",
    }
