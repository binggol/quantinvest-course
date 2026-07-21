from __future__ import annotations

from scripts.rdagent_backup.live_topk_dropout import (
    capped_topk_dropout,
    select_previous_holdings,
    topk_dropout_transition,
)


def _run(
    *,
    as_of: str,
    generated_at: str,
    codes: list[str],
    model: str = "lgb",
    universe: str = "csi300",
    batch: str = "batch-a",
    mode: str = "factor",
    status: str | None = "success",
    n_universe: int = 300,
    topk: int = 3,
    n_drop: int = 1,
) -> dict:
    row = {
        "as_of": as_of,
        "generated_at": generated_at,
        "model": model,
        "universe": universe,
        "batch": batch,
        "mode": mode,
        "n_universe": n_universe,
        "strategy": {
            "name": "TopkDropoutStrategy",
            "topk": topk,
            "n_drop": n_drop,
            "method_buy": "top",
            "method_sell": "bottom",
        },
        "portfolio": {"target": codes},
        "hits": [{"code": code, "rank": i + 1} for i, code in enumerate(codes)],
    }
    if status is not None:
        row["status"] = status
    return row


def test_first_run_initializes_the_full_topk() -> None:
    result = topk_dropout_transition(
        [("A", 5), ("B", 4), ("C", 3), ("D", 2)],
        [],
        topk=3,
        n_drop=1,
    )

    assert result.initialized is True
    assert result.retained == ()
    assert result.sold == ()
    assert result.added == ("A", "B", "C")
    assert result.target == ("A", "B", "C")


def test_qlib_style_dropout_replaces_only_the_combined_bottom() -> None:
    result = topk_dropout_transition(
        [("D", 10), ("A", 9), ("B", 8), ("C", 1)],
        ["A", "B", "C"],
        topk=3,
        n_drop=1,
    )

    assert result.retained == ("A", "B")
    assert result.sold == ("C",)
    assert result.added == ("D",)
    assert result.target == ("D", "A", "B")


def test_dropout_does_not_sell_a_better_holding_to_buy_a_worse_candidate() -> None:
    result = topk_dropout_transition(
        [("A", 10), ("B", 9), ("C", 8), ("D", 7)],
        ["A", "B", "C"],
        topk=3,
        n_drop=1,
    )

    assert result.sold == ()
    assert result.added == ()
    assert result.target == ("A", "B", "C")


def test_shared_capped_transition_matches_the_live_target() -> None:
    ranking = ["D", "A", "B", "C", "E"]
    expected = capped_topk_dropout(
        ["A", "B", "C"], ranking, topn=3, max_replacements=1
    )
    live = topk_dropout_transition(
        [(code, len(ranking) - rank) for rank, code in enumerate(ranking)],
        ["A", "B", "C"],
        topk=3,
        n_drop=1,
    )

    assert list(live.target) == expected


def test_history_selection_is_strictly_previous_and_exact_identity() -> None:
    valid = _run(
        as_of="2026-07-17",
        generated_at="2026-07-17 18:00:00",
        codes=["A", "B", "C"],
    )
    history = [
        # A same-day rerun is not an executed position for another same-day run.
        _run(
            as_of="2026-07-18",
            generated_at="2026-07-18 20:00:00",
            codes=["A", "B", "D"],
        ),
        # Newer, but a failed run cannot become portfolio state.
        _run(
            as_of="2026-07-17",
            generated_at="2026-07-17 22:00:00",
            codes=["A", "B", "D"],
            status="failed",
        ),
        # Cross-universe state must never leak into this sleeve.
        _run(
            as_of="2026-07-17",
            generated_at="2026-07-17 21:00:00",
            codes=["A", "B", "C"],
            universe="csi500",
        ),
        valid,
    ]

    selected = select_previous_holdings(
        history,
        current_as_of="2026-07-18",
        model="lgb",
        universe="csi300",
        batch="batch-a",
        mode="factor",
        topk=3,
        n_drop=1,
        current_universe_size=300,
        current_codes=["A", "B", "C", *[f"X{i}" for i in range(297)]],
    )

    assert selected.initialized is False
    assert selected.codes == ("A", "B", "C")
    assert selected.as_of == "2026-07-17"
    assert selected.rejection_counts == {
        "not_strictly_previous": 1,
        "unsuccessful": 1,
        "identity_mismatch": 1,
    }


def test_history_rejects_truncated_old_pool_and_low_overlap() -> None:
    history = [
        _run(
            as_of="2026-07-17",
            generated_at="2026-07-17 20:00:00",
            codes=["A", "B", "C"],
            universe="csi500",
            n_universe=50,
        ),
        _run(
            as_of="2026-07-16",
            generated_at="2026-07-16 20:00:00",
            codes=["OLD1", "OLD2", "OLD3"],
            universe="csi500",
            n_universe=500,
        ),
    ]

    selected = select_previous_holdings(
        history,
        current_as_of="2026-07-18",
        model="lgb",
        universe="csi500",
        batch="batch-a",
        mode="factor",
        topk=3,
        n_drop=1,
        current_universe_size=500,
        current_codes=["A", "B", "C", *[f"X{i}" for i in range(497)]],
    )

    assert selected.initialized is True
    assert selected.codes == ()
    assert selected.rejection_counts == {
        "universe_size_mismatch": 1,
        "low_code_overlap": 1,
    }


def test_history_falls_back_past_an_incompatible_strategy_snapshot() -> None:
    incompatible = _run(
        as_of="2026-07-17",
        generated_at="2026-07-17 20:00:00",
        codes=["A", "B", "D"],
        n_drop=2,
    )
    valid = _run(
        as_of="2026-07-16",
        generated_at="2026-07-16 20:00:00",
        codes=["A", "B", "C"],
    )

    selected = select_previous_holdings(
        [incompatible, valid],
        current_as_of="2026-07-18",
        model="lgb",
        universe="csi300",
        batch="batch-a",
        mode="factor",
        topk=3,
        n_drop=1,
        current_universe_size=300,
        current_codes=["A", "B", "C", "D", *[f"X{i}" for i in range(296)]],
    )

    assert selected.codes == ("A", "B", "C")
    assert selected.rejection_counts == {"strategy_mismatch": 1}


def test_legacy_successful_hits_can_bootstrap_but_alpha_cannot_consume_them() -> None:
    legacy = _run(
        as_of="2026-07-17",
        generated_at="2026-07-17 18:00:00",
        codes=["A", "B", "C"],
        mode="factor",
        status=None,
    )
    legacy.pop("portfolio")
    legacy.pop("strategy")
    legacy.pop("mode")

    common = dict(
        history_runs=[legacy],
        current_as_of="2026-07-18",
        model="lgb",
        universe="csi300",
        batch="batch-a",
        topk=3,
        n_drop=1,
        current_universe_size=300,
        current_codes=["A", "B", "C", *[f"X{i}" for i in range(297)]],
    )
    factor = select_previous_holdings(mode="factor", **common)
    alpha = select_previous_holdings(mode="alpha158", **common)

    assert factor.codes == ("A", "B", "C")
    assert factor.source_schema == "legacy_hits"
    assert alpha.initialized is True
    assert alpha.rejection_counts == {"identity_mismatch": 1}


def test_history_does_not_revive_a_stale_portfolio() -> None:
    stale = _run(
        as_of="2026-06-30",
        generated_at="2026-06-30 18:00:00",
        codes=["A", "B", "C"],
    )

    selected = select_previous_holdings(
        [stale],
        current_as_of="2026-07-18",
        model="lgb",
        universe="csi300",
        batch="batch-a",
        mode="factor",
        topk=3,
        n_drop=1,
        current_universe_size=300,
        current_codes=["A", "B", "C", *[f"X{i}" for i in range(297)]],
        maximum_calendar_gap_days=14,
    )

    assert selected.initialized is True
    assert selected.rejection_counts == {"stale_snapshot": 1}
