import json
import sys
from pathlib import Path

import app as app_module
from scripts import export_placement_events as placement_module
from scripts.export_placement_events import build_lifecycle, is_regulatory_approval


def _ann(code, date, title, announcement_id):
    return {
        "code": code,
        "date": date,
        "title": title,
        "announcement_id": announcement_id,
        "url": f"https://static.cninfo.com.cn/{announcement_id}.PDF",
    }


def test_build_lifecycle_keeps_regulatory_stages_evidence_backed():
    code = "601005"
    rows = [
        _ann(code, "2025-12-20", "2025年度向特定对象发行A股股票预案", "plan"),
        _ann(code, "2025-12-20", "第十届董事会第二十二次会议决议公告", "board"),
        _ann(code, "2026-01-05", "向特定对象发行股票获得中国宝武批复的公告", "owner"),
        _ann(code, "2026-03-14", "2026年第一次临时股东大会决议公告", "holder"),
        _ann(code, "2026-05-12", "向特定对象发行股票申请获得上海证券交易所审核通过的公告", "exchange"),
        _ann(code, "2026-06-04", "向特定对象发行股票申请获得中国证监会同意注册批复的公告", "csrc"),
        _ann(code, "2026-06-24", "向特定对象发行A股股票发行结果暨股本变动的公告", "issue"),
    ]

    result = build_lifecycle({
        "code": code,
        "name": "重庆钢铁",
        "issue_date": "2026-06-11",
        "list_date": "2026-06-22",
        "lock": "3年",
    }, rows)

    assert result["plan_date"] == "2025-12-20"
    assert result["board_date"] == "2025-12-20"
    assert result["shareholder_date"] == "2026-03-14"
    assert result["exchange_approval_date"] == "2026-05-12"
    assert result["approval_date"] == "2026-06-04"
    assert result["issue_date"] == "2026-06-11"
    assert result["stage_evidence"]["approval"]["announcement_id"] == "csrc"
    assert not is_regulatory_approval(rows[2]["title"])


def test_regulatory_approval_allows_company_name_inside_registration_phrase():
    assert is_regulatory_approval(
        "关于收到中国证券监督管理委员会《关于同意云南铜业股份有限公司"
        "发行股份购买资产并募集配套资金注册的批复》的公告"
    )


def test_build_lifecycle_uses_latest_plan_for_current_batch():
    code = "301160"
    rows = [
        _ann(code, "2022-01-10", "2022年度向特定对象发行股票预案", "old-plan"),
        _ann(code, "2022-08-01", "向特定对象发行股票获得中国证监会同意注册批复的公告", "old-approval"),
        _ann(code, "2025-08-12", "2025年度向特定对象发行股票预案", "new-plan"),
        _ann(code, "2025-09-12", "2025年第二次临时股东会决议公告", "new-holder"),
        _ann(code, "2026-04-08", "向特定对象发行股票获得中国证监会同意注册批复的公告", "new-approval"),
    ]
    result = build_lifecycle({"code": code, "list_date": "2026-07-01"}, rows)
    assert result["plan_date"] == "2025-08-12"
    assert result["shareholder_date"] == "2025-09-12"
    assert result["approval_date"] == "2026-04-08"


def test_lifecycle_uses_original_plan_and_shareholder_vote_before_exchange_review():
    code = "003013"
    rows = [
        _ann(code, "2022-08-23", "关于非公开发行股票申请获得中国证监会核准批复的公告", "old-approval"),
        _ann(code, "2025-01-10", "发行股份购买资产并募集配套资金暨关联交易预案", "plan"),
        _ann(code, "2025-01-10", "第六届董事会第十次会议决议公告", "board"),
        _ann(code, "2025-02-10", "2025年第一次临时股东会决议公告", "holder"),
        _ann(code, "2025-06-05", "发行股份购买资产报告书（草案）与预案差异对比说明", "comparison"),
        _ann(code, "2025-12-12", "关于发行股份购买资产事项获深圳证券交易所审核通过的公告", "exchange"),
        _ann(code, "2025-12-12", "发行股份购买资产报告书（草案）（注册稿）", "registered-draft"),
        _ann(code, "2025-12-16", "2025年第四次临时股东会决议公告", "unrelated-holder"),
        _ann(code, "2026-01-05", "发行股份购买资产获得中国证监会同意注册批复的公告", "approval"),
    ]

    result = build_lifecycle({"code": code, "issue_date": "2026-02-03", "list_date": "2026-02-12"}, rows)

    assert result["plan_date"] == "2025-01-10"
    assert result["board_date"] == "2025-01-10"
    assert result["shareholder_date"] == "2025-02-10"
    assert result["exchange_approval_date"] == "2025-12-12"
    assert result["approval_date"] == "2026-01-05"


def test_placement_unlock_requires_explicit_evidence():
    pending_term = app_module._placement_unlock_evidence({
        "dataset": "asset_injection.json",
        "list_date": "2026-06-22",
        "lock": "3年",
    })
    assert pending_term["unlock_date"] == ""
    assert pending_term["unlock_estimated"] is False
    assert pending_term["unlock_basis"] == "pending_source_evidence"

    estimated = app_module._placement_unlock_evidence({
        "dataset": "cninfo_placement.json",
        "lock_tranches": [{
            "period": "36个月",
            "scope": "asset_consideration",
            "basis": "issue_end_date",
            "lock_start_date": "2026-06-22",
        }],
        "lock_start_date": "2026-06-22",
        "lock_start_basis": "issue_end_date",
        "lock_term_confidence": "high",
    })
    assert estimated["unlock_date"] == "2029-06-22"
    assert estimated["unlock_estimated"] is True
    assert estimated["unlock_basis"] == "source_lock_period"
    assert estimated["unlock_confidence"] == "high"

    explicit = app_module._placement_unlock_evidence({
        "dataset": "official.json",
        "unlock_date": "2030-01-02",
        "list_date": "2026-06-22",
        "lock": "3年",
    })
    assert explicit["unlock_date"] == "2030-01-02"
    assert explicit["unlock_basis"] == "explicit_unlock_date"
    assert explicit["unlock_estimated"] is False

    pending = app_module._placement_unlock_evidence({
        "dataset": "placement.json",
        "list_date": "2026-06-22",
    })
    assert pending["unlock_date"] == ""
    assert pending["unlock_basis"] == "pending_source_evidence"
    assert pending["unlock_confidence"] == "pending"


def test_placement_stage_does_not_treat_every_announcement_as_plan():
    values = app_module._pt_stage_dates({
        "ann_date": "2026-06-04",
        "title": "向特定对象发行股票申请获得中国证监会同意注册批复的公告",
    }, "定增")
    plan, board, shareholder, exchange, approval, issue = values
    assert plan == ""
    assert board == ""
    assert shareholder == ""
    assert exchange == ""
    assert approval == "2026-06-04"
    assert issue == ""


def test_placement_api_merges_lifecycle_by_batch_and_reports_source_time(monkeypatch):
    payloads = {
        "cninfo_placement.json": {
            "updated": "2026-07-11 20:00:00",
            "items": [{
                "code": "601005",
                "name": "重庆钢铁",
                "plan_date": "2025-12-20",
                "board_date": "2025-12-20",
                "shareholder_date": "2026-03-14",
                "exchange_approval_date": "2026-05-12",
                "approval_date": "2026-06-04",
                "issue_date": "2026-06-11",
                "list_date": "2026-06-22",
                "stage_evidence": {"plan": {"announcement_id": "plan"}},
            }],
        },
        "asset_injection.json": {
            "updated": "2026-06-28 20:49",
            "items": [{
                "code": "601005",
                "name": "重庆钢铁",
                "list_date": "2026-06-22",
                "issue_price": 1.32,
                "lock": "3年",
            }],
        },
    }
    loaded = []

    def fake_load(name):
        loaded.append(name)
        return payloads.get(name)

    monkeypatch.setattr(app_module, "_eventrisk_load_json", fake_load)
    monkeypatch.setattr(app_module, "_placement_market_context", lambda code: {})
    monkeypatch.setattr(app_module, "_meta_for_codes", lambda codes: {})
    app_module.app.config.update(TESTING=True)

    response = app_module.app.test_client().get("/api/placement_events")
    assert response.status_code == 200
    data = response.get_json()
    assert data["updated"] == "2026-07-11 20:00:00"
    assert data["count"] == 1
    row = data["items"][0]
    assert row["issue_date"] == "2026-06-11"
    assert row["list_date"] == "2026-06-22"
    assert row["unlock_date"] == ""
    assert row["unlock_estimated"] is False
    assert row["lock_period"] == "3年"
    assert row["stage_evidence"]["plan"]["announcement_id"] == "plan"
    assert "cninfo_transfer.json" not in loaded


def test_placement_template_is_automatic_and_labels_estimates():
    text = Path("templates/placement_events.html").read_text(encoding="utf-8")
    assert "/api/refresh/transfer_events" not in text
    assert "refreshData()" not in text
    assert "解禁日/区间" in text
    assert "期限依据" in text
    assert "综合判断" in text
    assert "数据更新" in text


def test_parse_lock_terms_keeps_multiple_tranches_and_explicit_start_date():
    text = (
        "本次发行结束日为2025年12月30日。"
        "南京紫金投资集团认购的本次发行股份自发行结束之日起60个月内不得转让；"
        "产业投资者认购的本次发行股份自发行结束之日起36个月内不得转让；"
        "其他发行对象认购的本次发行股份自发行结束之日起6个月内不得转让。"
    )

    parsed = placement_module.parse_placement_lock_terms([text])

    assert parsed["issue_end_date"] == "2025-12-30"
    assert [row["months"] for row in parsed["lock_periods"]] == [60, 36, 6]
    assert all(row["start_event"] == "issue_end_date" for row in parsed["lock_periods"])
    assert all(row["lock_start_date"] == "2025-12-30" for row in parsed["lock_periods"])
    assert parsed["lock_periods"][0]["holder_or_group"] == "南京紫金投资集团认购的本次发行股份"


def test_lock_clause_without_start_date_never_uses_announcement_date():
    parsed = placement_module.parse_placement_lock_terms([
        "本次发行对象认购的新增股份限售期为十八个月。"
    ])
    term = parsed["lock_periods"][0]

    assert term["months"] == 18
    assert term["start_event"] == "not_explicit_in_clause"
    assert term["lock_start_date"] == ""


def test_flattened_table_header_does_not_turn_row_number_into_one_month_lock():
    parsed = placement_module.parse_placement_lock_terms([
        "本次发行配售结果如下:序号认购对象获配股份数限售期(月)1某机构100000006"
    ])

    assert parsed["lock_periods"] == []


def test_resolver_rejects_a_lock_start_date_from_an_old_batch():
    official = {
        "lock_periods": [{
            "months": 6,
            "period": "6个月",
            "lock_period": "6个月",
            "start_event": "listing_date",
            "lock_start_date": "2020-10-22",
            "scope": "cash_subscription",
        }],
        "listing_dates": ["2020-10-22"],
    }

    result = placement_module.resolve_lock_term_evidence(
        {
            "issue_date": "2026-02-03",
            "list_date": "2026-02-12",
            "list_date_source": "eastmoney:RPT_SEO_DETAIL.ISSUE_LISTING_DATE",
        },
        official,
        eastmoney_lock_period="36个月",
    )

    assert result["lock_term_source"] == "eastmoney:RPT_SEO_DETAIL.LOCKIN_PERIOD"
    assert result["lock_period"] == "36个月"
    assert result["lock_tranches"][0]["months"] == 36


def test_issue_document_extracts_csrc_approval_date_as_stage_evidence():
    parsed = placement_module.parse_placement_lock_terms([
        "2025年12月31日,公司收到中国证券监督管理委员会出具的"
        "《关于同意公司发行股份购买资产并募集配套资金注册的批复》。"
    ])

    assert parsed["regulatory_approval_date"] == "2025-12-31"
    assert parsed["regulatory_approval_date_evidence"][0]["page"] == 1


def test_mixed_asset_and_supporting_finance_tranches_keep_separate_scopes():
    parsed = placement_module.parse_placement_lock_terms([
        "发行股份购买资产的交易对方认购的本次发行股份"
        "自发行结束之日起36个月内不得转让；"
        "募集配套资金发行对象认购的本次发行股份"
        "自发行结束之日起6个月内不得转让。"
    ])

    assert [(row["months"], row["scope"]) for row in parsed["lock_periods"]] == [
        (36, "asset_consideration"),
        (6, "supporting_finance"),
    ]


def test_pre_existing_share_commitment_is_excluded_and_duplicate_evidence_is_merged():
    page_one = (
        "三、新增股份的限售安排华宝投资通过本次发行获得的公司新发行股份，"
        "自本次发行结束之日起36个月内不得转让。"
        "同时，华宝投资及中国宝武承诺，华宝投资及其一致行动人"
        "自本次发行结束之日起18个月内不转让本次发行前已持有的重庆钢铁股份。"
    )
    page_two = (
        "（七）限售期安排华宝投资通过本次发行获得的公司新发行股份，"
        "自本次发行结束之日起36个月内不得转让。"
        "同时，华宝投资及中国宝武承诺，华宝投资及其一致行动人"
        "自本次发行结束之日起18个月内不转让本次发行前已持有的重庆钢铁股份。"
    )

    parsed = placement_module.parse_placement_lock_terms([page_one, page_two])

    assert len(parsed["lock_periods"]) == 1
    assert parsed["lock_periods"][0]["months"] == 36
    assert parsed["lock_periods"][0]["applies_to_new_shares"] is True
    assert parsed["lock_periods"][0]["evidence_count"] == 2
    assert len(parsed["related_commitments"]) == 1
    related = parsed["related_commitments"][0]
    assert related["months"] == 18
    assert related["applies_to_new_shares"] is False
    assert related["applicable_shares"] == "pre_existing_shares"
    assert related["term_category"] == "related_commitment"
    assert related["evidence_count"] == 2


def test_cross_pdf_duplicate_terms_merge_but_keep_all_evidence(monkeypatch):
    code = "601005"
    page = (
        "华宝投资通过本次发行获得的公司新发行股份"
        "自本次发行结束之日起36个月内不得转让。"
        "华宝投资及其一致行动人自本次发行结束之日起18个月内"
        "不转让本次发行前已持有的重庆钢铁股份。"
    )
    rows = [
        {
            "code": code,
            "date": "2026-06-23",
            "title": "向特定对象发行A股股票发行情况报告书",
            "announcement_id": "doc-1",
            "url": "https://static.cninfo.com.cn/finalpage/2026-06-23/1225000101.PDF",
        },
        {
            "code": code,
            "date": "2026-06-24",
            "title": "向特定对象发行A股股票上市公告书",
            "announcement_id": "doc-2",
            "url": "https://static.cninfo.com.cn/finalpage/2026-06-24/1225000102.PDF",
        },
    ]
    monkeypatch.setattr(placement_module, "download_cninfo_pdf", lambda *args, **kwargs: b"%PDF-test")
    monkeypatch.setattr(placement_module, "extract_pdf_pages", lambda _content: [page])
    official = placement_module.collect_official_lock_evidence(
        {"code": code, "list_date": "2026-06-22"},
        rows,
        {
            "plan_date": "2025-12-20",
            "approval_date": "2026-06-04",
            "stage_evidence": {"issue": {"date": "2026-06-24"}},
        },
        timeout=1,
    )
    resolved = placement_module.resolve_lock_term_evidence({"code": code}, official)

    assert len(official["lock_periods"]) == 1
    assert official["lock_periods"][0]["months"] == 36
    assert official["lock_periods"][0]["evidence_count"] == 2
    assert {row["announcement_id"] for row in official["lock_periods"][0]["evidence_items"]} == {
        "doc-1", "doc-2",
    }
    assert len(resolved["lock_tranches"]) == 1
    assert resolved["lock_tranches"][0]["months"] == 36
    assert len(resolved["related_commitments"]) == 1
    assert resolved["related_commitments"][0]["months"] == 18
    assert resolved["related_commitments"][0]["applies_to_new_shares"] is False


def test_resolve_terms_prefers_official_tranches_and_marks_source_conflict():
    parsed = placement_module.parse_placement_lock_terms([
        "本次发行结束日为2026年1月2日。"
        "控股股东认购的本次发行股份自发行结束之日起18个月内不得转让。"
    ])
    official = {
        "lock_periods": parsed["lock_periods"],
        "documents": [{"status": "parsed", "announcement_id": "official-1"}],
        "issue_end_dates": [parsed["issue_end_date"]],
        "listing_dates": [],
        "tradable_dates": [],
        "date_conflict": False,
    }

    result = placement_module.resolve_lock_term_evidence(
        {"code": "600001"},
        official,
        eastmoney_lock_period="36个月",
    )

    assert result["lock_term_source"] == "cninfo_official_pdf"
    assert result["lock_period"] == "18个月"
    assert result["lock_tranches"][0]["months"] == 18
    assert result["lock_start_date"] == "2026-01-02"
    assert result["lock_term_conflict"] is True
    assert result["lock_term_conflict_detail"]["period_source_mismatch"] == {
        "cninfo_official_pdf_months": [18],
        "eastmoney_or_seed_months": [36],
    }


def test_missing_announcement_terms_do_not_receive_a_current_rule_default():
    result = placement_module.resolve_lock_term_evidence(
        {"code": "600001", "issue_date": "2018-01-01"},
        {"lock_periods": [], "documents": [], "issue_end_dates": [], "listing_dates": []},
    )

    assert result["lock_period"] == ""
    assert result["lock_periods"] == []
    assert result["lock_term_source"] == "pending_official_evidence"
    assert result["lock_term_confidence"] == "pending"
    assert result["lock_term_policy"] == "batch_announcement_evidence_only_no_current_rule_backfill"


def test_cninfo_pdf_url_and_download_limits_are_strict():
    valid = "https://static.cninfo.com.cn/finalpage/2026-07-10/1225419815.PDF"
    assert placement_module.validate_cninfo_pdf_url(valid) == valid
    for invalid in (
        "http://static.cninfo.com.cn/finalpage/2026-07-10/1225419815.PDF",
        "https://evil.example/finalpage/2026-07-10/1225419815.PDF",
        "https://static.cninfo.com.cn/other/1225419815.PDF",
        f"{valid}?next=https://evil.example",
    ):
        try:
            placement_module.validate_cninfo_pdf_url(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe URL accepted: {invalid}")

    class OversizedResponse:
        status_code = 200
        headers = {"Content-Length": str(placement_module.MAX_PDF_BYTES + 1)}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield b"%PDF-small-body"

    class Session:
        def get(self, *args, **kwargs):
            return OversizedResponse()

    try:
        placement_module.download_cninfo_pdf(valid, timeout=1, session=Session())
    except ValueError as exc:
        assert "size limit" in str(exc)
    else:
        raise AssertionError("oversized PDF was accepted")


def test_pdf_parse_error_degrades_to_eastmoney_secondary(monkeypatch):
    code = "601005"
    row = {
        "code": code,
        "date": "2026-06-24",
        "title": "向特定对象发行A股股票发行情况报告书暨上市公告书",
        "announcement_id": "1225419815",
        "url": "https://static.cninfo.com.cn/finalpage/2026-06-24/1225419815.PDF",
    }
    lifecycle = {
        "plan_date": "2025-12-20",
        "approval_date": "2026-06-04",
    }

    def broken_pdf(*args, **kwargs):
        raise ValueError("encrypted or malformed PDF")

    monkeypatch.setattr(placement_module, "download_cninfo_pdf", broken_pdf)
    official = placement_module.collect_official_lock_evidence(
        {"code": code, "list_date": "2026-06-22"},
        [row],
        lifecycle,
        timeout=1,
    )
    result = placement_module.resolve_lock_term_evidence(
        {"code": code},
        official,
        eastmoney_lock_period="6个月",
    )

    assert official["documents"][0]["status"] == "error"
    assert official["lock_periods"] == []
    assert result["lock_term_source"] == "eastmoney:RPT_SEO_DETAIL.LOCKIN_PERIOD"
    assert result["lock_term_confidence"] == "medium"
    assert result["lock_period"] == "6个月"


def test_pdf_candidates_are_limited_to_the_current_installment():
    code = "601005"
    old = {
        "code": code,
        "date": "2026-02-01",
        "title": "向特定对象发行A股股票发行情况报告书",
        "announcement_id": "old-batch",
        "url": "https://static.cninfo.com.cn/finalpage/2026-02-01/1225000001.PDF",
    }
    current = {
        "code": code,
        "date": "2026-06-24",
        "title": "向特定对象发行A股股票发行情况报告书暨上市公告书",
        "announcement_id": "current-batch",
        "url": "https://static.cninfo.com.cn/finalpage/2026-06-24/1225000002.PDF",
    }
    lifecycle = {
        "plan_date": "2025-12-20",
        "approval_date": "2026-01-10",
        "stage_evidence": {"issue": {"date": "2026-06-24"}},
    }

    selected = placement_module._current_batch_issue_documents(
        {"code": code, "list_date": "2026-06-22"},
        [old, current],
        lifecycle,
    )

    assert [row["announcement_id"] for row in selected] == ["current-batch"]


def test_lifecycle_ids_are_stable_and_issue_date_is_not_publication_date():
    code = "601005"
    rows = [
        _ann(code, "2025-12-20", "向特定对象发行A股股票预案", "plan-1"),
        _ann(code, "2026-06-04", "向特定对象发行A股股票获得中国证监会同意注册批复的公告", "approval-1"),
        _ann(code, "2026-06-24", "向特定对象发行A股股票发行结果暨股本变动公告", "issue-1"),
    ]
    without_issue_date = build_lifecycle({"code": code, "list_date": "2026-06-22"}, rows)
    with_eastmoney_date = build_lifecycle({
        "code": code,
        "issue_date": "2026-06-11",
        "issue_date_source": "eastmoney:RPT_SEO_DETAIL.ISSUE_DATE",
        "list_date": "2026-06-22",
    }, rows)

    assert without_issue_date["project_id"] == f"{code}:project:plan-1"
    assert without_issue_date["batch_id"] == f"{code}:batch:issue-1"
    assert without_issue_date["issue_date"] == ""
    assert without_issue_date["issue_announcement_date"] == "2026-06-24"
    assert with_eastmoney_date["issue_date"] == "2026-06-11"
    assert with_eastmoney_date["issue_date_source"] == "eastmoney:RPT_SEO_DETAIL.ISSUE_DATE"


def test_single_eastmoney_failure_is_warning_and_does_not_reject_seed(tmp_path, monkeypatch):
    seed = tmp_path / "asset_injection.json"
    seed.write_text(json.dumps({
        "items": [{
            "code": "601005",
            "name": "重庆钢铁",
            "list_date": "2026-06-22",
            "issue_price": 1.32,
            "lock": "3年",
        }],
    }, ensure_ascii=False), encoding="utf-8")

    def failed_eastmoney(self, row):
        raise RuntimeError("temporary Eastmoney timeout")

    monkeypatch.setattr(placement_module.PlacementClient, "eastmoney_record", failed_eastmoney)
    monkeypatch.setattr(placement_module.PlacementClient, "collect", lambda self, row: [])
    monkeypatch.setattr(sys, "argv", [
        "export_placement_events.py",
        "--data-dir", str(tmp_path),
        "--sleep", "0",
    ])

    placement_module.main()

    payload = json.loads((tmp_path / "cninfo_placement.json").read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["errors"] == []
    assert payload["warnings"][0]["source"] == "eastmoney"
    assert payload["items"][0]["code"] == "601005"
