"""本機資料庫（SQLite）：記兩種東西。

1. checks          每一次「去來源確認有沒有改版」的紀錄：何時查、成功與否、有沒有變。
2. clause_versions 每一個抓到的條文版本：版本日期、來源網址、擷取時間、內容指紋。

兩張表都是「只能新增」：資料庫裡設了觸發器（trigger），
任何 UPDATE 或 DELETE 都會被資料庫直接拒絕。
所以舊版本不會被新版本蓋掉，日後可以回查任一天當時的條文。
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id              TEXT    NOT NULL,   -- 哪個法源
    checked_at             TEXT    NOT NULL,   -- 何時檢查（UTC）
    ok                     INTEGER NOT NULL,   -- 1 = 成功連上來源；0 = 失敗
    changed                INTEGER,            -- 1 = 比上次成功檢查時新；0 = 沒變；空 = 無從比較
    source_last_amended_on TEXT,               -- 來源自己公告的「最後修訂日」
    detail                 TEXT    NOT NULL DEFAULT ''   -- 補充說明或錯誤訊息
);

CREATE TABLE IF NOT EXISTS clause_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    TEXT NOT NULL,              -- 哪個法源
    clause_id    TEXT NOT NULL,              -- 條號（例：211.22、6-1）
    citation     TEXT NOT NULL,              -- 完整引用寫法（例：21 CFR 211.22、藥事法 第 6-1 條）
    heading      TEXT NOT NULL DEFAULT '',   -- 條文標題或所在章節
    version_date TEXT NOT NULL,              -- 版本日期 YYYY-MM-DD（來源未提供時為空字串）
    content      TEXT NOT NULL,              -- 條文原文
    checksum     TEXT NOT NULL,              -- 內容指紋（sha256）
    source_url   TEXT NOT NULL,              -- 這段原文是從哪個網址抓的
    fetched_at   TEXT NOT NULL,              -- 何時抓的（UTC）
    UNIQUE (source_id, clause_id, version_date, checksum)
);

CREATE INDEX IF NOT EXISTS idx_versions_lookup
    ON clause_versions (source_id, clause_id, version_date);
CREATE INDEX IF NOT EXISTS idx_checks_source ON checks (source_id, id);

CREATE TRIGGER IF NOT EXISTS clause_versions_no_update BEFORE UPDATE ON clause_versions
BEGIN SELECT RAISE(ABORT, 'clause_versions 只能新增，不可修改'); END;
CREATE TRIGGER IF NOT EXISTS clause_versions_no_delete BEFORE DELETE ON clause_versions
BEGIN SELECT RAISE(ABORT, 'clause_versions 只能新增，不可刪除'); END;
CREATE TRIGGER IF NOT EXISTS checks_no_update BEFORE UPDATE ON checks
BEGIN SELECT RAISE(ABORT, 'checks 只能新增，不可修改'); END;
CREATE TRIGGER IF NOT EXISTS checks_no_delete BEFORE DELETE ON checks
BEGIN SELECT RAISE(ABORT, 'checks 只能新增，不可刪除'); END;
"""


def now_utc() -> str:
    """現在時間（UTC），存進資料庫用。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_local(iso: str | None) -> str | None:
    """把資料庫裡的 UTC 時間轉成這台電腦的當地時間（帶時區），給人看用。"""
    if not iso:
        return None
    return datetime.fromisoformat(iso).astimezone().isoformat(timespec="seconds")


def checksum_of(heading: str, content: str) -> str:
    """內容指紋：同樣的文字永遠算出同樣的指紋，改一個字指紋就不同。"""
    digest = hashlib.sha256(f"{heading}\n{content}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class Store:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """每次操作開一條新連線、做完就關，簡單而且不怕多執行緒。"""
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── 檢查紀錄 ────────────────────────────────────────────────

    def record_check(
        self,
        source_id: str,
        ok: bool,
        source_last_amended_on: str | None = None,
        detail: str = "",
    ) -> dict[str, Any]:
        """記下一次檢查。`changed` 是和「上一次成功的檢查」相比得出的。"""
        with self._conn() as conn:
            previous = conn.execute(
                "SELECT source_last_amended_on FROM checks "
                "WHERE source_id = ? AND ok = 1 ORDER BY id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            changed: int | None = None
            if ok and previous is not None:
                changed = int(previous["source_last_amended_on"] != source_last_amended_on)
            checked_at = now_utc()
            conn.execute(
                "INSERT INTO checks (source_id, checked_at, ok, changed, "
                "source_last_amended_on, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (source_id, checked_at, int(ok), changed, source_last_amended_on, detail),
            )
        return {
            "source_id": source_id,
            "checked_at": checked_at,
            "ok": bool(ok),
            "changed": None if changed is None else bool(changed),
            "source_last_amended_on": source_last_amended_on,
            "detail": detail,
        }

    def last_check(self, source_id: str, only_ok: bool = False) -> dict[str, Any] | None:
        """最近一次檢查（only_ok=True 時只看成功的）。"""
        where = "source_id = ?" + (" AND ok = 1" if only_ok else "")
        return self._one(f"SELECT * FROM checks WHERE {where} ORDER BY id DESC LIMIT 1", (source_id,))

    def last_change(self, source_id: str) -> dict[str, Any] | None:
        """最近一次「檢查時發現來源改版了」的紀錄。"""
        return self._one(
            "SELECT * FROM checks WHERE source_id = ? AND changed = 1 ORDER BY id DESC LIMIT 1",
            (source_id,),
        )

    def count_checks(self, source_id: str) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM checks WHERE source_id = ?", (source_id,))
        return int(row["n"]) if row else 0

    # ── 條文版本 ────────────────────────────────────────────────

    def add_version(
        self,
        source_id: str,
        clause_id: str,
        citation: str,
        heading: str,
        version_date: str,
        content: str,
        source_url: str,
    ) -> tuple[dict[str, Any], bool]:
        """存入一個條文版本。回傳（資料列, 是不是新的一列）。

        同一條、同一版本日期、同樣內容 → 已經存過，不重複存。
        內容不同 → 新增一列（舊的那列原封不動留著）。
        """
        checksum = checksum_of(heading, content)
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO clause_versions (source_id, clause_id, citation, heading, "
                "version_date, content, checksum, source_url, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, clause_id, citation, heading, version_date, content, checksum,
                 source_url, now_utc()),
            )
            is_new = cursor.rowcount == 1
            row = conn.execute(
                "SELECT * FROM clause_versions WHERE source_id = ? AND clause_id = ? "
                "AND version_date = ? AND checksum = ?",
                (source_id, clause_id, version_date, checksum),
            ).fetchone()
        return dict(row), is_new

    def get_version(
        self, source_id: str, clause_id: str, as_of: str | None = None
    ) -> dict[str, Any] | None:
        """取出某條在某日（as_of）當時的版本；不給日期就取最新的。"""
        sql = "SELECT * FROM clause_versions WHERE source_id = ? AND clause_id = ?"
        params: list[Any] = [source_id, clause_id]
        if as_of:
            sql += " AND version_date <= ?"
            params.append(as_of)
        return self._one(sql + " ORDER BY version_date DESC, id DESC LIMIT 1", params)

    def count_contents(self, source_id: str, clause_id: str, version_date: str) -> int:
        """同一條、同一版本日期下，存了幾種不同內容（正常應該是 1）。"""
        row = self._one(
            "SELECT COUNT(*) AS n FROM clause_versions "
            "WHERE source_id = ? AND clause_id = ? AND version_date = ?",
            (source_id, clause_id, version_date),
        )
        return int(row["n"]) if row else 0

    def version_dates(self, source_id: str) -> list[str]:
        """這個法源在本機存了哪些版本日期（新到舊）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT version_date FROM clause_versions "
                "WHERE source_id = ? ORDER BY version_date DESC",
                (source_id,),
            ).fetchall()
        return [row["version_date"] for row in rows]

    def snapshot(self, source_id: str, version_date: str) -> list[dict[str, Any]]:
        """某個版本日期下，這個法源的全部條文（每條取最後存入的那筆）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM clause_versions WHERE id IN ("
                "  SELECT MAX(id) FROM clause_versions "
                "  WHERE source_id = ? AND version_date = ? GROUP BY clause_id"
                ") ORDER BY id",
                (source_id, version_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_versions(self, source_id: str) -> int:
        """本機存了幾筆條文版本（不含「_」開頭的附屬紀錄，例如沿革）。"""
        row = self._one(
            "SELECT COUNT(*) AS n FROM clause_versions "
            "WHERE source_id = ? AND substr(clause_id, 1, 1) != '_'",
            (source_id,),
        )
        return int(row["n"]) if row else 0

    def _one(self, sql: str, params: Any) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
