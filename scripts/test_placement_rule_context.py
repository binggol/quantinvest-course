import app as app_module


def test_unlock_uses_evidenced_issue_date_and_supports_multiple_terms():
    result = app_module._placement_unlock_evidence({
        "dataset": "cninfo_placement.json",
        "issue_date": "2026-06-11",
        "issue_date_source": "eastmoney:RPT_SEO_DETAIL.ISSUE_DATE",
        "issue_end_date": "2026-06-11",
        "list_date": "2026-06-22",
        "lock_tranches": [
            {"period": "6个月", "start_event": "issue_end_date"},
            {"period": "18个月", "start_event": "issue_end_date"},
        ],
        "lock_term_confidence": "high",
    })

    assert result["lock_start_date"] == "2026-06-11"
    assert result["unlock_date"] == "2026-12-11"
    assert result["unlock_date_latest"] == "2027-12-11"
    assert result["unlock_basis"] == "tranche_schedule"
    assert result["unlock_schedule"] == [
        {
            "lock_period": "6个月", "unlock_date": "2026-12-11", "scope": "unknown",
            "estimated": True, "lock_start_date": "2026-06-11",
            "start_event": "issue_end_date", "start_confidence": "high",
        },
        {
            "lock_period": "18个月", "unlock_date": "2027-12-11", "scope": "unknown",
            "estimated": True, "lock_start_date": "2026-06-11",
            "start_event": "issue_end_date", "start_confidence": "high",
        },
    ]


def test_unlock_does_not_use_listing_date_as_unproven_start_date():
    result = app_module._placement_unlock_evidence({
        "dataset": "legacy.json",
        "list_date": "2026-06-22",
        "lock_period": "3年",
    })

    assert result["unlock_date"] == ""
    assert result["unlock_basis"] == "pending_source_evidence"
    assert result["unlock_confidence"] == "pending"


def test_official_issue_end_clause_can_use_structured_issue_date_as_labeled_proxy():
    result = app_module._placement_unlock_evidence({
        "dataset": "cninfo_placement.json",
        "issue_date": "2026-06-11",
        "issue_date_source": "eastmoney:RPT_SEO_DETAIL.ISSUE_DATE",
        "lock_tranches": [{
            "period": "36个月",
            "start_event": "issue_end_date",
            "applies_to_new_shares": True,
        }],
        "lock_term_confidence": "high",
    })

    assert result["unlock_date"] == "2029-06-11"
    assert result["unlock_confidence"] == "medium"
    assert result["unlock_schedule"][0]["start_event"] == "issue_date_proxy_for_issue_end"


def test_official_tradable_date_overrides_estimate_and_conditional_term_stays_pending():
    official = app_module._placement_unlock_evidence({
        "dataset": "official.json",
        "tradable_date_official": "2028-01-03",
        "issue_date": "2026-01-01",
        "issue_date_source": "eastmoney:RPT_SEO_DETAIL.ISSUE_DATE",
        "lock_period": "18个月",
    })
    assert official["unlock_date"] == "2028-01-03"
    assert official["unlock_estimated"] is False
    assert official["unlock_basis"] == "official_tradable_date"

    conditional = app_module._placement_unlock_evidence({
        "dataset": "official.json",
        "issue_date": "2026-01-01",
        "issue_date_source": "eastmoney:RPT_SEO_DETAIL.ISSUE_DATE",
        "lock_tranches": [{"period": "36个月", "conditional": True}],
    })
    assert conditional["unlock_date"] == ""
    assert conditional["unlock_basis"] == "pending_source_evidence"


def test_rule_review_is_version_aware_and_never_backfills_a_term():
    missing_date = app_module._placement_regulatory_context({}, [])
    assert missing_date["status"] == "pending_date_evidence"
    assert missing_date["expected_months"] == []

    historical = app_module._placement_regulatory_context(
        {"code": "600001", "issue_date": "2019-01-02"},
        [{"lock_period": "12个月", "months": 12, "days": None}],
    )
    assert historical["expected_months"] == [12, 36]
    assert historical["status"] == "consistent"

    current = app_module._placement_regulatory_context(
        {"code": "300001", "issue_date": "2026-01-02"},
        [{"lock_period": "6个月", "months": 6, "days": None}],
    )
    assert current["expected_months"] == [6, 18]
    assert current["status"] == "consistent"

    bse = app_module._placement_regulatory_context(
        {"code": "920001", "issue_date": "2026-01-02"},
        [{"lock_period": "12个月", "months": 12, "days": None}],
    )
    assert bse["expected_months"] == [6, 12]
    assert bse["status"] == "consistent"


def test_restructuring_terms_remain_announcement_controlled():
    context = app_module._placement_regulatory_context(
        {
            "code": "600001",
            "issue_date": "2026-01-02",
            "title": "发行股份购买资产暨关联交易实施情况报告书",
        },
        [{"lock_period": "36个月", "months": 36, "days": None}],
    )

    assert context["transaction_type"] == "restructuring"
    assert context["expected_months"] == [6, 18]
    assert {rule["scope"] for rule in context["applicable_rules"]} == {
        "base_issuance", "restructuring", "special_or_commitment",
    }
    assert context["status"] == "announcement_controls"


def test_mixed_project_reviews_each_tranche_separately():
    context = app_module._placement_regulatory_context(
        {
            "code": "600001",
            "issue_date": "2026-01-02",
            "title": "发行股份购买资产并募集配套资金实施情况报告书",
        },
        [
            {"lock_period": "36个月", "months": 36, "days": None, "scope": "asset_consideration"},
            {"lock_period": "6个月", "months": 6, "days": None, "scope": "supporting_finance"},
        ],
    )

    assert context["transaction_type"] == "mixed"
    assert context["status"] == "mixed_tranches_announcement_controls"
    assert [row["status"] for row in context["tranche_reviews"]] == [
        "announcement_controls", "consistent",
    ]


def test_placement_merge_prefers_official_evidence_and_keeps_conflicts():
    rows = [
        {
            "dataset": "placement_status.json",
            "code": "600001",
            "plan_date": "2024-01-01",
            "issue_date": "2026-06-12",
            "list_date": "2026-06-22",
        },
        {
            "dataset": "cninfo_placement.json",
            "lifecycle_source": "cninfo_official_pdf",
            "project_id": "plan-123",
            "batch_id": "issue-456",
            "code": "600001",
            "plan_date": "2025-01-01",
            "issue_date": "2026-06-11",
            "list_date": "2026-06-22",
            "stage_evidence": {"plan": {"announcement_id": "plan-123"}},
        },
        {
            "dataset": "asset_injection.json",
            "code": "600001",
            "issue_date": "2026-06-11",
            "list_date": "2026-06-22",
            "lock_period": "3年",
        },
    ]

    merged = app_module._pt_merge_placement_rows(rows)
    assert len(merged) == 1
    assert merged[0]["project_id"] == "plan-123"
    assert merged[0]["plan_date"] == "2025-01-01"
    assert merged[0]["issue_date"] == "2026-06-11"
    assert merged[0]["lock_period"] == "3年"
    assert {conflict["field"] for conflict in merged[0]["field_conflicts"]} >= {
        "plan_date", "issue_date",
    }


def test_placement_merge_does_not_guess_between_parallel_projects():
    rows = [
        {"dataset": "cninfo_placement.json", "project_id": "p1", "batch_id": "b1", "code": "600001", "list_date": "2026-06-22"},
        {"dataset": "cninfo_placement.json", "project_id": "p2", "batch_id": "b2", "code": "600001", "list_date": "2026-06-22"},
        {"dataset": "asset_injection.json", "code": "600001", "list_date": "2026-06-22", "lock_period": "3年"},
    ]

    merged = app_module._pt_merge_placement_rows(rows)
    assert len(merged) == 3
