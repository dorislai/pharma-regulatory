"""本機資料庫的測試：版本只新增不覆蓋、檢查紀錄正確。"""

import sqlite3

import pytest

from pharma_reg_mcp.store import Store, checksum_of


def add(store: Store, content: str, version_date: str = "2025-05-27"):
    return store.add_version(
        "us-21cfr", "211.52", "21 CFR 211.52", "§ 211.52 Heading.", version_date, content,
        "https://example.test/source",
    )


def test_same_content_is_stored_only_once(store: Store):
    first, first_is_new = add(store, "text A")
    second, second_is_new = add(store, "text A")
    assert first_is_new is True
    assert second_is_new is False
    assert first["id"] == second["id"]
    assert first["fetched_at"] == second["fetched_at"]  # 保留第一次擷取的時間
    assert store.count_versions("us-21cfr") == 1


def test_changed_content_is_added_and_old_one_is_kept(store: Store):
    old, _ = add(store, "old text", "2016-11-29")
    new, is_new = add(store, "new text", "2025-05-27")
    assert is_new is True
    assert old["checksum"] != new["checksum"]
    assert store.count_versions("us-21cfr") == 2
    # 舊版本還在，而且內容沒有被動過
    assert store.get_version("us-21cfr", "211.52", as_of="2020-01-01")["content"] == "old text"


def test_get_version_returns_the_one_in_force_on_a_date(store: Store):
    add(store, "old text", "2016-11-29")
    add(store, "new text", "2025-05-27")
    assert store.get_version("us-21cfr", "211.52")["content"] == "new text"
    assert store.get_version("us-21cfr", "211.52", as_of="2025-05-26")["content"] == "old text"
    assert store.get_version("us-21cfr", "211.52", as_of="2025-05-27")["content"] == "new text"
    assert store.get_version("us-21cfr", "211.52", as_of="2010-01-01") is None
    assert store.get_version("us-21cfr", "999.9") is None


def test_checksum_is_stable_and_sensitive(store: Store):
    row, _ = add(store, "text A")
    assert row["checksum"] == checksum_of("§ 211.52 Heading.", "text A")
    assert row["checksum"].startswith("sha256:")
    assert checksum_of("h", "text A") != checksum_of("h", "text B")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE clause_versions SET content = 'tampered'",
        "DELETE FROM clause_versions",
        "UPDATE checks SET ok = 1",
        "DELETE FROM checks",
    ],
)
def test_records_cannot_be_modified_or_deleted(store: Store, tmp_path, sql: str):
    """直接對資料庫下修改或刪除指令，資料庫本身就會拒絕。"""
    add(store, "text A")
    store.record_check("us-21cfr", True, "2026-11-12")
    conn = sqlite3.connect(tmp_path / "test.db")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="只能新增"):
            conn.execute(sql)
    finally:
        conn.close()
    assert store.get_version("us-21cfr", "211.52")["content"] == "text A"


def test_check_records_whether_the_source_changed(store: Store):
    first = store.record_check("us-21cfr", True, "2026-11-12")
    assert first["changed"] is None  # 第一次檢查，沒有前一次可以比

    same = store.record_check("us-21cfr", True, "2026-11-12")
    assert same["changed"] is False

    failed = store.record_check("us-21cfr", False, None, "連線失敗")
    assert failed["changed"] is None  # 失敗的檢查不能下「有沒有變」的結論

    newer = store.record_check("us-21cfr", True, "2026-11-20")
    assert newer["changed"] is True  # 和上一次「成功」的檢查比，不是和失敗那次比

    assert store.count_checks("us-21cfr") == 4
    assert store.last_check("us-21cfr")["source_last_amended_on"] == "2026-11-20"
    assert store.last_change("us-21cfr")["source_last_amended_on"] == "2026-11-20"


def test_last_check_can_skip_failures(store: Store):
    store.record_check("us-21cfr", True, "2026-11-12")
    store.record_check("us-21cfr", False, None, "連線失敗")
    assert store.last_check("us-21cfr")["ok"] == 0
    assert store.last_check("us-21cfr", only_ok=True)["source_last_amended_on"] == "2026-11-12"
    assert store.last_check("tw-pharmaceutical-affairs-act") is None


def test_snapshot_and_version_dates(store: Store):
    for clause_id, content in (("4", "第四條"), ("5", "第五條"), ("_history", "沿革")):
        store.add_version("tw", clause_id, f"法 第 {clause_id} 條", "", "2026-03-04", content, "u")
    store.add_version("tw", "4", "法 第 4 條", "", "2027-01-01", "第四條（新）", "u")
    assert store.version_dates("tw") == ["2027-01-01", "2026-03-04"]
    assert [r["clause_id"] for r in store.snapshot("tw", "2026-03-04")] == ["4", "5", "_history"]
    assert store.count_versions("tw") == 3  # 「_」開頭的附屬紀錄不算條文
