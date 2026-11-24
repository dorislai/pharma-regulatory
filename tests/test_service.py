"""四個工具的行為測試。重點是三條規則有沒有守住：

1. 每個回傳都帶出處（條號、版本日期、來源網址、擷取時間）。
2. 查不到就說查不到，不給條文內容。
3. 連不上來源時明講「無法確認是否最新」，不默默沿用舊資料。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import pharma_reg_mcp.service as service_module
from conftest import (
    ACT_ARTICLES,
    FakeHttp,
    VERSIONS_211_52,
    add_ecfr_211_52,
    add_tw_act,
    ecfr_titles,
    ecfr_version,
    ecfr_versions,
    fixture_bytes,
    tw_law,
    tw_page,
)
from pharma_reg_mcp.http import NotFound, SourceUnavailable
from pharma_reg_mcp.service import RegulationService
from pharma_reg_mcp.store import Store

US = "us-21cfr"
ACT = "tw-pharmaceutical-affairs-act"
DOWN = SourceUnavailable("連線失敗（ConnectTimeout）：https://example.test")

PROVENANCE_KEYS = {"version_date", "as_of", "source_url", "web_url", "retrieved_at", "checksum"}


def assert_full_provenance(result: dict, confirmed: bool = True) -> None:
    """規則 1：有條文內容的回傳，出處欄位必須齊全而且有值。

    唯一的例外：連不上來源時（confirmed=False），沒有指定日期的查詢其 as_of 是空的——
    因為無法確認，程式不可以宣稱「這是今天有效的版本」。
    """
    assert result["citation"] and result["clause_id"]
    assert PROVENANCE_KEYS <= set(result["provenance"])
    required = PROVENANCE_KEYS if confirmed else PROVENANCE_KEYS - {"as_of"}
    assert all(result["provenance"][key] for key in required)
    assert result["provenance"]["source_url"].startswith("https://")
    assert result["provenance"]["checksum"].startswith("sha256:")


def assert_no_content(result: dict) -> None:
    """規則 2：查不到或取不到時，回傳裡不可以有任何條文內容。"""
    assert "text" not in result
    assert "不要用模型自己的記憶" in result["message"]


# ══ get_clause：美國 21 CFR ═════════════════════════════════════


def test_ecfr_clause_comes_with_full_provenance(service: RegulationService, http: FakeHttp):
    add_ecfr_211_52(http)
    result = service.get_clause(US, "21 CFR 211.52")
    assert result["status"] == "ok"
    assert result["citation"] == "21 CFR 211.52"
    assert result["heading"] == "§ 211.52 Washing and toilet facilities."
    assert "easily accesible to working areas" in result["text"]
    assert_full_provenance(result)
    assert result["provenance"]["version_date"] == "2025-05-27"
    assert result["provenance"]["as_of"] == "2026-11-20"
    assert "full/2026-11-20/title-21.xml?part=211&section=211.52" in result["provenance"]["source_url"]
    assert result["freshness"]["confirmed"] is True
    assert result["warnings"] == []


def test_ecfr_as_of_returns_the_version_in_force_on_that_date(
    service: RegulationService, http: FakeHttp, store: Store
):
    add_ecfr_211_52(http)
    current = service.get_clause(US, "211.52")
    past = service.get_clause(US, "211.52", as_of="2025-04-01")
    assert past["status"] == "ok"
    assert past["provenance"]["version_date"] == "2016-11-29"
    assert past["provenance"]["as_of"] == "2025-04-01"
    assert "easily accessible to working areas" in past["text"]
    assert past["text"] != current["text"]
    assert past["provenance"]["checksum"] != current["provenance"]["checksum"]
    # 兩個版本都留在本機，新版沒有蓋掉舊版
    assert store.count_versions(US) == 2


def test_ecfr_missing_clause_is_reported_as_not_found(service: RegulationService, http: FakeHttp):
    http.add("titles.json", ecfr_titles())
    http.add("versions/title-21.json", ecfr_versions([]))
    http.add("full/", NotFound("No matching content found."))
    result = service.get_clause(US, "211.9999")
    assert result["status"] == "not_found"
    assert "查不到" in result["message"]
    assert "No matching content found." in result["message"]
    assert_no_content(result)
    assert result["provenance"]["source_url"].endswith("section=211.9999")
    assert result["provenance"]["retrieved_at"]


def test_ecfr_removed_clause_is_reported_as_not_found(service: RegulationService, http: FakeHttp):
    http.add("titles.json", ecfr_titles())
    http.add("versions/title-21.json", ecfr_versions([
        ecfr_version("211.52", "2016-11-29"),
        ecfr_version("211.52", "2026-06-01", removed=True),
    ]))
    result = service.get_clause(US, "211.52")
    assert result["status"] == "not_found"
    assert "已於 2026-06-01 移除" in result["message"]
    assert_no_content(result)


def test_ecfr_down_without_local_copy_gives_no_content(
    service: RegulationService, http: FakeHttp, store: Store
):
    http.add("ecfr.gov", DOWN)
    result = service.get_clause(US, "211.52")
    assert result["status"] == "source_unavailable"
    assert "無法確認是否最新" in result["message"]
    assert_no_content(result)
    assert store.last_check(US)["ok"] == 0  # 失敗的檢查也要留紀錄


def test_ecfr_down_with_local_copy_is_flagged_unverified(
    service: RegulationService, http: FakeHttp
):
    """規則 3：連不上來源時可以給本機舊資料，但必須明講無法確認是否最新。"""
    add_ecfr_211_52(http)
    fresh = service.get_clause(US, "211.52")
    http.add("ecfr.gov", DOWN)

    result = service.get_clause(US, "211.52")
    assert result["status"] == "unverified"
    assert result["text"] == fresh["text"]
    assert result["freshness"]["confirmed"] is False
    assert "無法確認是否最新" in result["freshness"]["note"]
    assert any("連不上來源" in w and "重新查詢確認" in w for w in result["warnings"])
    assert_full_provenance(result, confirmed=False)
    assert result["provenance"]["as_of"] is None  # 無法確認，所以不宣稱是哪一天有效的版本
    # 擷取時間是「當初真正抓到的時間」，不是現在
    assert result["provenance"]["retrieved_at"] == fresh["provenance"]["retrieved_at"]


def test_ecfr_failure_midway_also_falls_back_honestly(service: RegulationService, http: FakeHttp):
    add_ecfr_211_52(http)
    service.get_clause(US, "211.52")
    http.add("full/", DOWN)  # 版本查得到，但全文抓不到
    assert service.get_clause(US, "211.52")["status"] == "unverified"


def test_ecfr_as_of_later_than_source_data_is_clamped_with_a_warning(
    service: RegulationService, http: FakeHttp
):
    add_ecfr_211_52(http)
    today = date.today().isoformat()
    result = service.get_clause(US, "211.52", as_of=today)
    assert result["status"] == "ok"
    assert result["provenance"]["as_of"] == "2026-11-20"
    assert any("只更新到 2026-11-20" in w for w in result["warnings"])


def test_ecfr_silent_content_change_under_same_version_date_is_flagged(
    service: RegulationService, http: FakeHttp, store: Store
):
    add_ecfr_211_52(http)
    service.get_clause(US, "211.52")
    changed = fixture_bytes("ecfr_211_52_current.xml").replace(b"hot and cold", b"warm")
    http.add("full/2026-11-20/title-21.xml", changed)
    result = service.get_clause(US, "211.52")
    assert result["status"] == "ok"
    assert any("同一個版本日期下" in w for w in result["warnings"])
    assert store.count_contents(US, "211.52", "2025-05-27") == 2  # 兩份都留著


def test_ecfr_paragraph_reference_returns_whole_section_with_a_note(
    service: RegulationService, http: FakeHttp
):
    add_ecfr_211_52(http)
    result = service.get_clause(US, "211.52(a)")
    assert result["clause_id"] == "211.52"
    assert any("以整條為單位" in w for w in result["warnings"])


# ══ get_clause：台灣法規 ════════════════════════════════════════


def test_tw_clause_comes_with_full_provenance(service: RegulationService, http: FakeHttp):
    add_tw_act(http)
    result = service.get_clause(ACT, "第 6-1 條")
    assert result["status"] == "ok"
    assert result["clause_id"] == "6-1"
    assert result["citation"] == "藥事法 第 6-1 條"
    assert result["heading"] == "第 一 章 總則"
    assert result["text"].startswith("經中央衛生主管機關公告類別之藥品")
    assert_full_provenance(result)
    assert result["provenance"]["version_date"] == "2026-03-04"
    assert result["provenance"]["source_url"] == "https://law.moj.gov.tw/api/Ch/Law/JSON"
    assert result["provenance"]["web_url"].endswith("LawSingle.aspx?pcode=L0030001&flno=6-1")
    assert result["freshness"]["confirmed"] is True
    assert "law_effective_note" not in result


def test_tw_recent_check_is_reused_instead_of_asking_the_source_again(
    service: RegulationService, http: FakeHttp, store: Store
):
    add_tw_act(http)
    first = service.get_clause(ACT, "4")
    calls_after_first = len(http.calls)
    second = service.get_clause(ACT, "5")
    assert second["status"] == "ok"
    assert len(http.calls) == calls_after_first  # 幾分鐘內確認過，不重複連線
    assert second["freshness"]["checked_at"] == first["freshness"]["checked_at"]
    assert store.count_checks(ACT) == 1


def test_tw_unknown_article_and_too_early_date_are_not_found(
    service: RegulationService, http: FakeHttp
):
    add_tw_act(http)
    missing = service.get_clause(ACT, "999")
    assert missing["status"] == "not_found"
    assert "沒有第 999 條" in missing["message"]
    assert_no_content(missing)

    too_early = service.get_clause(ACT, "4", as_of="2020-01-01")
    assert too_early["status"] == "not_found"
    assert "只提供現行版" in too_early["message"]
    assert_no_content(too_early)


def test_tw_effective_note_is_passed_through(service: RegulationService, http: FakeHttp):
    note = "一百十五年三月四日修正公布之第 27-2 條條文，依第 10\r\n6 條規定：施行日期由行政院定之。"
    add_tw_act(http, law=tw_law(effective_note=note))
    result = service.get_clause(ACT, "4")
    assert result["law_effective_note"] == (
        "一百十五年三月四日修正公布之第 27-2 條條文，依第 106 條規定：施行日期由行政院定之。"
    )
    assert any("生效內容" in w for w in result["warnings"])


def test_tw_bulk_data_lagging_behind_website_is_flagged_outdated(
    service: RegulationService, http: FakeHttp
):
    """官網說 2026-03-04 修正了，但整包開放資料還停在 2013 年的版本。"""
    add_tw_act(http, law=tw_law(modified="20130730"), page=tw_page(115, 3, 4))
    result = service.get_clause(ACT, "4")
    assert result["status"] == "outdated"
    assert result["provenance"]["version_date"] == "2013-07-30"
    assert result["freshness"]["confirmed"] is False
    assert "無法確認是否最新" in result["freshness"]["note"]
    assert any("已於 2026-03-04 修正" in w for w in result["warnings"])
    # 但回查新版公布之前的日期時，手上這份就是正確的版本
    past = service.get_clause(ACT, "4", as_of="2020-01-01")
    assert past["status"] == "ok"
    assert past["warnings"] == []


def test_tw_down_without_local_copy_gives_no_content(service: RegulationService, http: FakeHttp):
    http.add("law.moj.gov.tw", DOWN)
    result = service.get_clause(ACT, "4")
    assert result["status"] == "source_unavailable"
    assert_no_content(result)


def test_tw_bulk_download_failure_on_first_use_gives_no_content(
    service: RegulationService, http: FakeHttp
):
    http.add("LawAll.aspx", tw_page())
    http.add("api/Ch/Law/JSON", DOWN)
    result = service.get_clause(ACT, "4")
    assert result["status"] == "source_unavailable"
    assert_no_content(result)


def test_tw_down_with_local_copy_is_flagged_unverified(
    service: RegulationService, http: FakeHttp, monkeypatch: pytest.MonkeyPatch
):
    add_tw_act(http)
    service.get_clause(ACT, "4")
    monkeypatch.setattr(service_module, "TW_RECHECK_AFTER_SECONDS", 0)  # 讓它必須重新確認
    http.add("law.moj.gov.tw", DOWN)
    result = service.get_clause(ACT, "4")
    assert result["status"] == "unverified"
    assert result["text"] == "本法所稱藥物，係指藥品及醫療器材。"
    assert result["freshness"]["confirmed"] is False
    assert any("無法確認" in w for w in result["warnings"])


def test_tw_new_version_is_added_and_old_version_stays_retrievable(
    service: RegulationService, http: FakeHttp, store: Store
):
    """法規改版後：新舊版本並存，可逐條比對，也能回查舊版。"""
    add_tw_act(http, law=tw_law(modified="20130730"), page=tw_page(102, 7, 30))
    assert service.get_clause(ACT, "4")["provenance"]["version_date"] == "2013-07-30"

    # ↓ 以下修正內容是測試用的虛構文字
    revised = [
        ("C", "", "   第 一 章 總則"),
        ("A", "第 4 條", "本法所稱藥物，指藥品及醫療器材。（測試用虛構修正）"),
        ("A", "第 5 條", ACT_ARTICLES[2][2]),
        ("A", "第 7 條", "（測試用虛構新增條文）"),
    ]
    add_tw_act(http, law=tw_law(modified="20260304", articles=revised), page=tw_page(115, 3, 4))

    updates = service.check_updates(ACT, "2025-01-01")
    assert updates["status"] == "ok"
    assert updates["has_updates"] is True
    assert updates["check"]["changed_since_last_check"] is True
    assert updates["items"][0]["level"] == "law"
    assert updates["items"][0]["version_date"] == "2026-03-04"
    changes = {i["clause_id"]: i["change_type"] for i in updates["items"] if i["level"] == "clause"}
    assert changes == {"4": "修正", "7": "新增", "6-1": "刪除", "61": "刪除"}
    assert "逐條比對" in updates["clause_level_detail"]

    latest = service.get_clause(ACT, "4")
    assert latest["provenance"]["version_date"] == "2026-03-04"
    assert "虛構修正" in latest["text"]

    old = service.get_clause(ACT, "4", as_of="2020-01-01")
    assert old["status"] == "ok"
    assert old["provenance"]["version_date"] == "2013-07-30"
    assert old["text"] == "本法所稱藥物，係指藥品及醫療器材。"
    assert store.version_dates(ACT) == ["2026-03-04", "2013-07-30"]


# ══ check_updates ═══════════════════════════════════════════════


def test_check_updates_ecfr_lists_changes_and_records_the_check(
    service: RegulationService, http: FakeHttp, store: Store
):
    http.add("titles.json", ecfr_titles())
    http.add("versions/title-21.json", ecfr_versions([
        ecfr_version("211.52", "2025-05-27"),
        ecfr_version("211.1", "2025-12-18"),
        ecfr_version("211.999", "2025-08-01", removed=True),
    ]))
    result = service.check_updates(US, "2025-01-01", scope="211")
    assert result["status"] == "ok"
    assert result["has_updates"] is True
    assert result["total_changes"] == 3
    assert [i["clause_id"] for i in result["items"]] == ["211.1", "211.999", "211.52"]  # 新到舊
    first = result["items"][0]
    assert first["citation"] == "21 CFR 211.1"
    assert first["version_date"] == "2025-12-18"
    assert first["web_url"] == "https://www.ecfr.gov/on/2025-12-18/title-21/section-211.1"
    assert result["items"][1]["removed"] is True and result["items"][1]["web_url"] is None
    assert "part=211&issue_date[gte]=2025-01-01" in result["provenance"]["source_url"]
    assert result["provenance"]["retrieved_at"]
    assert result["provenance"]["source_last_amended_on"] == "2026-11-12"
    assert result["check"]["changed_since_last_check"] is None  # 第一次檢查
    assert store.count_checks(US) == 1


def test_check_updates_ecfr_truncates_long_lists_and_summarises_by_part(
    service: RegulationService, http: FakeHttp
):
    entries = [ecfr_version(f"{part}.{n}", f"2026-10-0{n}")
               for part in (211, 558) for n in range(1, 6)]
    http.add("titles.json", ecfr_titles())
    http.add("versions/title-21.json", ecfr_versions(entries))
    result = service.check_updates(US, "2026-10-01", max_items=3)
    assert result["total_changes"] == 10
    assert result["returned"] == 3
    assert "只列出最新的 3 筆" in result["message"]
    assert result["changes_by_part"] == {"211": 5, "558": 5}


def test_check_updates_no_changes(service: RegulationService, http: FakeHttp):
    http.add("titles.json", ecfr_titles())
    http.add("versions/title-21.json", ecfr_versions([]))
    result = service.check_updates(US, "2026-11-21", scope="211")
    assert result["status"] == "ok"
    assert result["has_updates"] is False
    assert result["items"] == []
    assert "沒有條文異動" in result["message"]


def test_check_updates_failure_is_not_reported_as_no_changes(
    service: RegulationService, http: FakeHttp, store: Store
):
    """規則 3：連不上來源 ≠ 沒有改版。"""
    http.add("titles.json", ecfr_titles())
    http.add("versions/title-21.json", ecfr_versions([]))
    service.check_updates(US, "2025-01-01")
    http.add("ecfr.gov", DOWN)

    result = service.check_updates(US, "2025-01-01")
    assert result["status"] == "source_unavailable"
    assert "無法確認是否最新" in result["message"]
    assert "has_updates" not in result and "items" not in result
    assert result["last_successful_check_at"]
    assert store.last_check(US)["ok"] == 0

    listed = next(s for s in service.list_sources()["sources"] if s["source_id"] == US)
    assert listed["last_check_ok"] is False
    assert listed["source_last_amended_on"] == "2026-11-12"  # 來自上一次成功的檢查
    assert any("無法確認是否最新" in w for w in service.list_sources()["warnings"])


def test_check_updates_tw_reports_law_level_change_with_official_note(
    service: RegulationService, http: FakeHttp
):
    add_tw_act(http)
    result = service.check_updates(ACT, "2025-01-01")
    assert result["status"] == "ok"
    assert result["has_updates"] is True
    item = result["items"][0]
    assert item["level"] == "law"
    assert item["version_date"] == "2026-03-04"
    assert "修正公布第 27-2、48-2、80、94、96-1、106 條條文" in item["amendment_note"]
    assert "無法用程式逐條比對" in result["clause_level_detail"]
    assert result["provenance"]["source_url"].endswith("LawAll.aspx?pcode=L0030001")
    assert result["provenance"]["bulk_data_updated_on"] == "2026-11-13"

    later = service.check_updates(ACT, "2026-06-01")
    assert later["has_updates"] is False
    assert later["items"] == []
    assert "沒有改版" in later["message"]


def test_check_updates_always_asks_the_source(service: RegulationService, http: FakeHttp):
    add_tw_act(http)
    service.check_updates(ACT, "2025-01-01")
    calls = len(http.calls)
    service.check_updates(ACT, "2025-01-01")
    assert len(http.calls) == calls + 1  # 改版檢查不走「幾分鐘內不重複確認」的捷徑


# ══ search_clauses ══════════════════════════════════════════════


def ecfr_search_payload(*sections: str) -> bytes:
    results = [
        {
            "type": "Section",
            "starts_on": "2016-11-29",
            "hierarchy": {"title": "21", "part": s.split(".")[0], "section": s},
            "headings": {"part": "Part heading", "section": f"Heading of {s}"},
            "full_text_excerpt": f"…<strong>quality</strong> control in {s}…",
        }
        for s in sections
    ]
    return json.dumps({"results": results, "meta": {"total_count": len(results)}}).encode()


def test_search_ecfr_results_carry_citation_version_and_url(
    service: RegulationService, http: FakeHttp
):
    http.add("titles.json", ecfr_titles())
    http.add("search/v1/results", ecfr_search_payload("900.12", "211.22", "211.165"))
    result = service.search_clauses("quality control", source_id=US, limit=2)
    assert result["status"] == "ok"
    assert [r["clause_id"] for r in result["results"]] == ["900.12", "211.22"]
    hit = result["results"][1]
    assert hit["citation"] == "21 CFR 211.22"
    assert hit["version_date"] == "2016-11-29"
    assert hit["excerpt"] == "…quality control in 211.22…"
    assert hit["web_url"] == "https://www.ecfr.gov/on/2026-11-20/title-21/section-211.22"
    assert hit["retrieved_at"]
    assert "date=2026-11-20" in result["sources_searched"][0]["provenance"]["source_url"]


def test_search_ecfr_scope_keeps_only_that_part(service: RegulationService, http: FakeHttp):
    http.add("titles.json", ecfr_titles())
    http.add("search/v1/results", ecfr_search_payload("900.12", "211.22", "211.165"))
    result = service.search_clauses("quality control", source_id=US, scope="211")
    assert [r["clause_id"] for r in result["results"]] == ["211.22", "211.165"]
    assert result["sources_searched"][0]["matched"] == 2


def test_search_tw_matches_all_keywords_and_ranks_by_frequency(
    service: RegulationService, http: FakeHttp
):
    add_tw_act(http)
    result = service.search_clauses("藥物", source_id=ACT)
    assert result["status"] == "ok"
    assert [r["clause_id"] for r in result["results"]] == ["5", "4"]  # 第 5 條出現兩次，排前面
    hit = result["results"][0]
    assert hit["citation"] == "藥事法 第 5 條"
    assert hit["version_date"] == "2026-03-04"
    assert "試驗用藥物" in hit["excerpt"]
    assert hit["web_url"].endswith("flno=5")
    assert hit["retrieved_at"]

    both = service.search_clauses("藥物 醫療器材", source_id=ACT)
    assert [r["clause_id"] for r in both["results"]] == ["4"]


def test_search_with_no_match_says_so(service: RegulationService, http: FakeHttp):
    add_tw_act(http)
    result = service.search_clauses("不存在的關鍵字", source_id=ACT)
    assert result["status"] == "ok"
    assert result["results"] == []
    assert "沒有找到" in result["message"]


def test_search_reports_partial_when_one_source_is_down(
    service: RegulationService, http: FakeHttp
):
    add_tw_act(http)
    http.add("LawAll.aspx?pcode=L0030073", DOWN)
    http.add("ecfr.gov", DOWN)
    result = service.search_clauses("藥物")
    assert result["status"] == "partial"
    statuses = {s["source_id"]: s["status"] for s in result["sources_searched"]}
    assert statuses == {
        US: "source_unavailable",
        ACT: "ok",
        "tw-drug-gmp-regulations": "source_unavailable",
    }
    assert {r["source_id"] for r in result["results"]} == {ACT}
    assert "無法確認是否最新" in result["message"]


def test_search_tw_on_stale_local_copy_is_flagged_unverified(
    service: RegulationService, http: FakeHttp, monkeypatch: pytest.MonkeyPatch
):
    add_tw_act(http)
    service.get_clause(ACT, "4")
    monkeypatch.setattr(service_module, "TW_RECHECK_AFTER_SECONDS", 0)
    http.add("law.moj.gov.tw", DOWN)
    result = service.search_clauses("藥物", source_id=ACT)
    assert result["status"] == "unverified"
    assert result["results"]
    assert any("無法確認是否最新" in w for w in result["sources_searched"][0]["warnings"])


# ══ list_sources 與參數檢查 ═════════════════════════════════════


def test_list_sources_before_and_after_checks(service: RegulationService, http: FakeHttp):
    before = service.list_sources()
    assert before["status"] == "ok"
    assert [s["source_id"] for s in before["sources"]] == [
        US, ACT, "tw-drug-gmp-regulations",
    ]
    assert all(s["last_checked_at"] is None for s in before["sources"])
    assert len(before["warnings"]) == 3  # 三個法源都還沒檢查過

    http.add("titles.json", ecfr_titles(amended="2026-11-12"))
    http.add("versions/title-21.json", ecfr_versions(VERSIONS_211_52))
    service.check_updates(US, "2025-01-01")
    http.add("titles.json", ecfr_titles(amended="2026-11-21"))  # 來源有了新的修訂
    service.check_updates(US, "2025-01-01")

    us = next(s for s in service.list_sources()["sources"] if s["source_id"] == US)
    assert us["last_checked_at"] and us["last_check_ok"] is True
    assert us["source_last_amended_on"] == "2026-11-21"
    assert us["last_change_detected_at"] == us["last_checked_at"]
    assert us["checks_recorded"] == 2


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda s: s.get_clause("no-such-source", "1"), "沒有登錄"),
        (lambda s: s.get_clause(US, "quality control"), "看不懂條號"),
        (lambda s: s.get_clause(ACT, "第五十七條"), "看不懂條號"),
        (lambda s: s.get_clause(US, "211.22", as_of="2025/04/01"), "YYYY-MM-DD"),
        (
            lambda s: s.get_clause(
                US, "211.22", as_of=(date.today() + timedelta(days=1)).isoformat()
            ),
            "未來的日期",
        ),
        (lambda s: s.check_updates(US, "去年"), "YYYY-MM-DD"),
        (lambda s: s.check_updates(US, "2025-01-01", scope="Part 211"), "scope 看不懂"),
        (lambda s: s.check_updates("no-such-source", "2025-01-01"), "沒有登錄"),
        (lambda s: s.search_clauses("   "), "不可以是空的"),
        (lambda s: s.search_clauses("x", source_id="no-such-source"), "沒有登錄"),
    ],
)
def test_invalid_input_is_rejected_without_contacting_the_source(
    service: RegulationService, http: FakeHttp, call, expected: str
):
    result = call(service)
    assert result["status"] == "invalid_input"
    assert expected in result["message"]
    assert http.calls == []
