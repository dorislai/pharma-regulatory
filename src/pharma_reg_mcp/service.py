"""四個查詢工具真正的邏輯（MCP 的外殼在 server.py）。

這裡守住三條規則：
1. 每個回傳都帶出處：條號、版本日期、來源網址、擷取時間。
2. 查不到就回「查不到」，不給任何條文內容。
3. 連不上來源時回報「無法確認是否最新」；就算拿本機舊資料出來，也一定明講。

回傳裡的 status 欄位一看就知道屬於哪種情況：
- ok                  向來源確認過，內容可用
- not_found           來源明確說沒有（或本機沒有那個日期的版本）
- unverified          連不上來源；附的是本機舊資料，無法確認是否最新
- outdated            來源已有新版，但還拿不到新版條文；附的是舊版
- source_unavailable  連不上來源，本機也沒有資料可給
- invalid_input       參數寫法有問題
- partial             （僅搜尋）部分法源成功、部分失敗
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .config import TW_RECHECK_AFTER_SECONDS
from .http import HttpClient, NotFound, SourceUnavailable
from .registry import SOURCES, Source
from .sources import ecfr, twlaw
from .store import Store, now_utc, to_local

NO_GUESS = "請如實告知使用者，不要用模型自己的記憶補上條文內容。"

# 台灣法規的「沿革」與「生效內容註記」跟著每個版本一起存，用特殊代號和一般條文區分。
HISTORY_ID = "_history"
EFFECTIVE_NOTE_ID = "_effective_note"


def _is_date(text: Any) -> bool:
    if not isinstance(text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _today() -> str:
    return date.today().isoformat()


def _seconds_since(utc_iso: str) -> float:
    """某個（UTC）時間點距離現在過了幾秒。"""
    return (datetime.now(timezone.utc) - datetime.fromisoformat(utc_iso)).total_seconds()


def _invalid(message: str) -> dict[str, Any]:
    return {"status": "invalid_input", "message": message}


def _check_view(check: dict[str, Any]) -> dict[str, Any]:
    """把一筆檢查紀錄整理成給人看的樣子。"""
    changed = check["changed"]
    return {
        "checked_at": to_local(check["checked_at"]),
        "ok": bool(check["ok"]),
        "changed_since_last_check": None if changed is None else bool(changed),
        "source_last_amended_on": check["source_last_amended_on"],
    }


@dataclass
class _TwSync:
    """一次「台灣法規同步」的結果。"""

    page_date: str                # 官網顯示的修正日期
    page_url: str
    local_date: str               # 本機最新版本的日期
    bulk_update_date: str | None  # 這次有下載整包資料的話，整包的更新日期
    lagging: bool                 # 官網已有新版，但本機還沒拿到
    check: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


class RegulationService:
    def __init__(self, store: Store, http: HttpClient) -> None:
        self.store = store
        self.http = http

    # ════════════════════════════════════════════════════════════
    # 工具 1：list_sources —— 有哪些法源、各自最後檢查與變更時間
    # ════════════════════════════════════════════════════════════

    def list_sources(self) -> dict[str, Any]:
        sources = []
        warnings = []
        for src in SOURCES.values():
            last = self.store.last_check(src.id)
            last_ok = self.store.last_check(src.id, only_ok=True)
            change = self.store.last_change(src.id)
            if last is None:
                warnings.append(f"{src.id}：還沒檢查過，先呼叫 check_updates 才知道是否最新。")
            elif not last["ok"]:
                warnings.append(f"{src.id}：最近一次檢查失敗，無法確認是否最新（{last['detail']}）。")
            sources.append(
                {
                    "source_id": src.id,
                    "name": src.name,
                    "jurisdiction": src.jurisdiction,
                    "language": src.language,
                    "provider": src.provider_name,
                    "homepage": src.homepage,
                    "clause_id_format": src.clause_id_hint,
                    "scope": src.scope_hint,
                    "last_checked_at": to_local(last["checked_at"]) if last else None,
                    "last_check_ok": bool(last["ok"]) if last else None,
                    "last_check_detail": last["detail"] if last else None,
                    "source_last_amended_on": (
                        last_ok["source_last_amended_on"] if last_ok else None
                    ),
                    "last_change_detected_at": to_local(change["checked_at"]) if change else None,
                    "checks_recorded": self.store.count_checks(src.id),
                    "versions_stored": self.store.count_versions(src.id),
                }
            )
        return {
            "status": "ok",
            "sources": sources,
            "warnings": warnings,
            "message": (
                f"已登錄 {len(sources)} 個法源。last_checked_at 是本工具最後一次向來源確認的時間；"
                "source_last_amended_on 是來源自己公告的最後修訂日（以最近一次成功檢查為準）；"
                "last_change_detected_at 是本工具最近一次在檢查時發現來源改版的時間。"
            ),
        }

    # ════════════════════════════════════════════════════════════
    # 工具 2：check_updates —— 某法源自某日起有哪些條文改版
    # ════════════════════════════════════════════════════════════

    def check_updates(
        self, source_id: str, since: str, scope: str | None = None, max_items: int = 50
    ) -> dict[str, Any]:
        src = SOURCES.get(source_id)
        if src is None:
            return self._unknown_source(source_id)
        if not _is_date(since):
            return _invalid(f"since 必須是 YYYY-MM-DD 格式的日期，收到的是「{since}」。")
        if since > _today():
            return _invalid(f"since（{since}）是未來的日期，無法查詢。")
        max_items = max(1, min(int(max_items), 500))
        scope = (scope or "").strip() or None
        if src.provider == "ecfr":
            return self._check_updates_ecfr(src, since, scope, max_items)
        return self._check_updates_tw(src, since)

    def _check_updates_ecfr(
        self, src: Source, since: str, part: str | None, max_items: int
    ) -> dict[str, Any]:
        if part and not re.fullmatch(r"\d{1,4}", part):
            return _invalid(f"scope 看不懂：「{part}」。{src.scope_hint}。")
        try:
            status = ecfr.fetch_title_status(self.http)
            versions = ecfr.fetch_versions(self.http, since=since, part=part)
        except NotFound as exc:
            return {
                "status": "not_found",
                "source_id": src.id,
                "message": f"來源回覆找不到這個查詢範圍（{exc}）。{NO_GUESS}",
            }
        except SourceUnavailable as exc:
            return self._unavailable(src, exc)
        check = self.store.record_check(
            src.id, True, status.latest_amended_on, f"eCFR 資料更新至 {status.up_to_date_as_of}"
        )

        entries = sorted(
            versions.entries,
            key=lambda e: (e.get("issue_date") or "", e.get("date") or ""),
            reverse=True,
        )
        items = []
        for entry in entries[:max_items]:
            identifier = str(entry.get("identifier") or "")
            is_section = entry.get("type") == "section"
            removed = bool(entry.get("removed"))
            items.append(
                {
                    "level": "clause",
                    "clause_id": identifier if is_section else None,
                    "citation": ecfr.citation(identifier),
                    "name": entry.get("name"),
                    "version_date": entry.get("date"),
                    "issue_date": entry.get("issue_date"),
                    "substantive": entry.get("substantive"),
                    "removed": removed,
                    "web_url": (
                        ecfr.web_url(identifier, entry["date"])
                        if is_section and not removed and entry.get("date")
                        else None
                    ),
                }
            )

        warnings = []
        if status.import_in_progress:
            warnings.append("eCFR 正在匯入新資料，稍後結果可能更新。")
        if versions.truncated:
            warnings.append("異動筆數太多，沒有全部抓完；請用 scope 指定 Part 或把 since 往後調。")
        total = versions.total
        scope_text = f"Part {part}" if part else "整個 Title 21"
        if total:
            message = f"{scope_text} 自 {since} 起（依 eCFR 發布日期）共有 {total} 筆條文版本異動。"
            if len(items) < total:
                message += f"以下只列出最新的 {len(items)} 筆。"
        else:
            message = (
                f"{scope_text} 自 {since} 起沒有條文異動"
                f"（eCFR 資料更新至 {status.up_to_date_as_of}）。"
            )
            if part:
                message += "如果 Part 編號寫錯，也會得到這個結果，請確認編號。"
        result: dict[str, Any] = {
            "status": "ok",
            "source_id": src.id,
            "since": since,
            "scope": part,
            "has_updates": total > 0,
            "total_changes": total,
            "returned": len(items),
            "items": items,
            "field_notes": (
                "version_date：eCFR 記錄的該版本日期；issue_date：該版本在 eCFR 發布的日期；"
                "substantive=false：來源標記為非實質變更；removed=true：該條文已移除。"
            ),
            "provenance": {
                "source_url": versions.url,
                "retrieved_at": to_local(now_utc()),
                "source_last_amended_on": status.latest_amended_on,
                "source_up_to_date_as_of": status.up_to_date_as_of,
            },
            "check": _check_view(check),
            "warnings": warnings,
            "message": message,
        }
        if part is None and total:
            counts = Counter(str(e.get("part")) for e in entries if e.get("part"))
            result["changes_by_part"] = dict(counts.most_common(20))
        return result

    def _check_updates_tw(self, src: Source, since: str) -> dict[str, Any]:
        try:
            sync = self._sync_tw(src, force=True)  # 改版檢查永遠即時查
        except SourceUnavailable as exc:
            return self._unavailable(src, exc)

        has_updates = sync.page_date >= since
        items: list[dict[str, Any]] = []
        clause_level_detail = None
        if has_updates:
            history = self.store.get_version(src.id, HISTORY_ID)
            note = ""
            if history and history["version_date"] == sync.page_date:
                note = twlaw.last_history_entry(history["content"])
            items.append(
                {
                    "level": "law",
                    "clause_id": None,
                    "citation": src.name,
                    "name": src.name,
                    "version_date": sync.page_date,
                    "amendment_note": note or None,
                    "web_url": sync.page_url,
                }
            )
            changed = self._diff_tw(src, since, sync)
            if changed is None:
                clause_level_detail = (
                    "無法用程式逐條比對：官方開放資料只提供現行版，本機沒有收錄 "
                    f"{since} 之前的版本可供對照。修了哪些條請看 amendment_note"
                    "（官方沿革原文）。從現在起本工具會保留每個版本，之後的改版就能逐條比對。"
                )
            else:
                items.extend(changed)
                clause_level_detail = "已用本機保留的前後版本逐條比對內容。"

        if has_updates:
            message = f"{src.name} 最近一次修正日期是 {sync.page_date}，在 {since} 之後，有改版。"
        else:
            message = f"{src.name} 自 {since} 起沒有改版（最近一次修正日期是 {sync.page_date}）。"
        return {
            "status": "ok",
            "source_id": src.id,
            "since": since,
            "scope": None,
            "has_updates": has_updates,
            "total_changes": len(items),
            "returned": len(items),
            "items": items,
            "clause_level_detail": clause_level_detail,
            "provenance": {
                "source_url": sync.page_url,
                "retrieved_at": to_local(sync.check["checked_at"]),
                "source_last_amended_on": sync.page_date,
                "local_version_date": sync.local_date,
                "bulk_data_updated_on": sync.bulk_update_date,
            },
            "check": _check_view(sync.check),
            "warnings": sync.warnings,
            "message": message,
        }

    def _diff_tw(self, src: Source, since: str, sync: _TwSync) -> list[dict[str, Any]] | None:
        """用本機保留的兩個版本逐條比對；沒有 since 之前的版本可比時回傳 None。"""
        dates = [d for d in self.store.version_dates(src.id) if d]
        baseline = next((d for d in dates if d < since), None)
        if sync.lagging or not dates or baseline is None:
            return None
        latest = dates[0]

        def articles(version_date: str) -> dict[str, dict[str, Any]]:
            return {
                row["clause_id"]: row
                for row in self.store.snapshot(src.id, version_date)
                if not row["clause_id"].startswith("_")
            }

        old, new = articles(baseline), articles(latest)
        changes = []
        for clause_id, row in new.items():
            before = old.get(clause_id)
            if before is not None and before["content"] == row["content"]:
                continue
            changes.append((clause_id, row, "新增" if before is None else "修正"))
        changes += [(cid, old[cid], "刪除") for cid in old.keys() - new.keys()]
        return [
            {
                "level": "clause",
                "clause_id": clause_id,
                "citation": row["citation"],
                "name": row["heading"],
                "version_date": latest,
                "change_type": change_type,
                "compared_with_version": baseline,
                "web_url": twlaw.article_url(src.pcode, clause_id),
            }
            for clause_id, row, change_type in changes
        ]

    # ════════════════════════════════════════════════════════════
    # 工具 3：get_clause —— 取得某條原文，可指定日期回查
    # ════════════════════════════════════════════════════════════

    def get_clause(
        self, source_id: str, clause_id: str, as_of: str | None = None
    ) -> dict[str, Any]:
        src = SOURCES.get(source_id)
        if src is None:
            return self._unknown_source(source_id)
        as_of = (as_of or "").strip() or None
        if as_of is not None:
            if not _is_date(as_of):
                return _invalid(f"as_of 必須是 YYYY-MM-DD 格式的日期，收到的是「{as_of}」。")
            if as_of > _today():
                return _invalid(f"as_of（{as_of}）是未來的日期，無法查詢。")
        if src.provider == "ecfr":
            return self._get_clause_ecfr(src, clause_id, as_of)
        return self._get_clause_tw(src, clause_id, as_of)

    def _get_clause_ecfr(self, src: Source, clause_id: str, as_of: str | None) -> dict[str, Any]:
        section = ecfr.normalize_section(clause_id)
        if not section:
            return _invalid(f"看不懂條號「{clause_id}」。寫法：{src.clause_id_hint}。")
        warnings = []
        if re.search(r"\(\w+\)", clause_id or ""):
            warnings.append(f"v0.1 以整條為單位回傳，以下是 {section} 全文，請在其中找指定的款項。")

        # 第一步：問來源「你的資料更新到哪一天」，同時也是一次改版檢查
        try:
            status = ecfr.fetch_title_status(self.http)
        except (SourceUnavailable, NotFound) as exc:
            return self._clause_from_cache(src, section, as_of, exc)
        target = as_of or status.up_to_date_as_of
        if target > status.up_to_date_as_of:
            warnings.append(
                f"eCFR 的資料目前只更新到 {status.up_to_date_as_of}，"
                f"已改用該日期查詢（原本指定 {as_of}）。"
            )
            target = status.up_to_date_as_of
        if status.import_in_progress:
            warnings.append("eCFR 正在匯入新資料，稍後內容可能更新。")
        check_detail = f"eCFR 資料更新至 {status.up_to_date_as_of}"

        # 第二步：查這一條的版本日期；第三步：抓那一天當時的原文
        try:
            versions = ecfr.fetch_versions(
                self.http, until=target, part=section.split(".")[0], section=section
            )
            picked = ecfr.pick_version(versions.entries, target)
            if picked and picked.get("removed"):
                raise NotFound(f"此條文已於 {picked['date']} 移除")
            fetched = ecfr.fetch_section(self.http, section, target)
        except NotFound as exc:
            check = self.store.record_check(src.id, True, status.latest_amended_on, check_detail)
            return {
                "status": "not_found",
                "source_id": src.id,
                "clause_id": section,
                "citation": ecfr.citation(section),
                "message": (
                    f"查不到：{target} 當時的 21 CFR 沒有 {section} 這一條"
                    f"（{exc}）。{NO_GUESS}"
                ),
                "provenance": {
                    "as_of": target,
                    "source_url": ecfr.section_url(section, target),
                    "retrieved_at": to_local(now_utc()),
                },
                "check": _check_view(check),
            }
        except SourceUnavailable as exc:
            return self._clause_from_cache(src, section, as_of, exc)

        check = self.store.record_check(src.id, True, status.latest_amended_on, check_detail)
        version_date = picked["date"] if picked else ""
        if not version_date:
            warnings.append("來源沒有提供這一條的版本日期。")
        row, is_new = self.store.add_version(
            src.id, section, ecfr.citation(section), fetched.heading, version_date,
            fetched.text, fetched.url,
        )
        if is_new and self.store.count_contents(src.id, section, version_date) > 1:
            warnings.append(
                "注意：同一個版本日期下，這次抓到的內容和先前收錄的不同"
                "（來源可能做了沒有標示日期的更動）。兩份內容都已保留在本機。"
            )
        return self._clause_result(
            src, row,
            status="ok",
            as_of=target,
            source_url=fetched.url,
            web_url=ecfr.web_url(section, target),
            retrieved_at=now_utc(),
            freshness={
                "confirmed": True,
                "checked_at": to_local(check["checked_at"]),
                "note": f"已向 eCFR 確認，這是 {target} 當時有效的版本"
                f"（eCFR 資料更新至 {status.up_to_date_as_of}）。",
            },
            warnings=warnings,
        )

    def _get_clause_tw(self, src: Source, clause_id: str, as_of: str | None) -> dict[str, Any]:
        article = twlaw.normalize_article_no(clause_id)
        if not article:
            return _invalid(f"看不懂條號「{clause_id}」。寫法：{src.clause_id_hint}。")
        try:
            sync = self._sync_tw(src)
        except SourceUnavailable as exc:
            return self._clause_from_cache(src, article, as_of, exc)

        cite = twlaw.citation(src.name, article)
        row = self.store.get_version(src.id, article, as_of)
        if row is None:
            if self.store.get_version(src.id, article) is None:
                message = f"查不到：{src.name}（{sync.local_date} 版）沒有第 {article} 條。"
            else:
                message = (
                    f"查不到 {cite} 在 {as_of} 當時的版本：官方開放資料只提供現行版，"
                    f"本機最早收錄的版本晚於 {as_of}，v0.1 無法回查更早的條文。"
                )
            return {
                "status": "not_found",
                "source_id": src.id,
                "clause_id": article,
                "citation": cite,
                "message": f"{message}{NO_GUESS}",
                "provenance": {
                    "as_of": as_of,
                    "source_url": sync.page_url,
                    "retrieved_at": to_local(sync.check["checked_at"]),
                    "source_last_amended_on": sync.page_date,
                },
                "check": _check_view(sync.check),
            }

        # 官網已有新版、而使用者要的正是「現在」或「新版日期之後」的條文 → 手上這份是舊的
        stale = sync.lagging and (as_of is None or as_of >= sync.page_date)
        warnings = list(sync.warnings) if stale else []
        if stale:
            note = (
                f"官網修正日期是 {sync.page_date}，但本機只有 {row['version_date']} 的版本，"
                "這一條可能已經修改，無法確認是否最新。"
            )
        else:
            when = f"{as_of} 當時" if as_of else "目前"
            note = (
                f"已向全國法規資料庫確認：官網修正日期 {sync.page_date}，"
                f"這是{when}適用的版本（版本日期 {row['version_date']}）。"
            )
        result = self._clause_result(
            src, row,
            status="outdated" if stale else "ok",
            as_of=as_of or _today(),
            source_url=row["source_url"],
            web_url=twlaw.article_url(src.pcode, article),
            retrieved_at=row["fetched_at"],
            freshness={
                "confirmed": not stale,
                "checked_at": to_local(sync.check["checked_at"]),
                "note": note,
            },
            warnings=warnings,
        )
        effective = self.store.get_version(src.id, EFFECTIVE_NOTE_ID, row["version_date"])
        if effective and effective["version_date"] == row["version_date"]:
            result["law_effective_note"] = effective["content"]
            result["warnings"].append(
                "這部法規有「生效內容」註記（見 law_effective_note）：部分條文的施行日期另定，"
                "引用前請確認這一條是否已經生效。"
            )
        return result

    def _clause_from_cache(
        self, src: Source, clause_id: str, as_of: str | None, exc: Exception
    ) -> dict[str, Any]:
        """連不上來源時的處理：有本機舊資料就給，但一定標示「無法確認是否最新」。"""
        check = self.store.record_check(src.id, False, None, str(exc))
        row = self.store.get_version(src.id, clause_id, as_of)
        if row is None:
            return {
                "status": "source_unavailable",
                "source_id": src.id,
                "clause_id": clause_id,
                "message": (
                    f"連不上來源（{src.provider_name}），本機也沒有收錄過這一條，"
                    f"所以無法提供內容，也無法確認是否最新。{NO_GUESS}"
                ),
                "error": str(exc),
                "check": _check_view(check),
            }
        fetched_at = to_local(row["fetched_at"])
        when = f"{as_of} 當時有效" if as_of else "最新"
        if src.provider == "ecfr":
            web = ecfr.web_url(clause_id, as_of or row["version_date"] or _today())
        else:
            web = twlaw.article_url(src.pcode, clause_id)
        return self._clause_result(
            src, row,
            status="unverified",
            as_of=as_of,
            source_url=row["source_url"],
            web_url=web,
            retrieved_at=row["fetched_at"],
            freshness={
                "confirmed": False,
                "checked_at": to_local(check["checked_at"]),
                "note": f"連不上來源，無法確認是否最新（{exc}）。",
            },
            warnings=[
                (
                    f"連不上來源，無法確認這是不是{when}的版本。以下是本機在 {fetched_at} "
                    f"擷取的內容（版本日期 {row['version_date'] or '不明'}），引用前請重新查詢確認。"
                )
            ],
        )

    def _clause_result(
        self,
        src: Source,
        row: dict[str, Any],
        *,
        status: str,
        as_of: str | None,
        source_url: str,
        web_url: str,
        retrieved_at: str,
        freshness: dict[str, Any],
        warnings: list[str],
    ) -> dict[str, Any]:
        """所有「有條文內容」的回傳都長同一個樣子。"""
        return {
            "status": status,
            "source_id": src.id,
            "clause_id": row["clause_id"],
            "citation": row["citation"],
            "heading": row["heading"],
            "text": row["content"],
            "provenance": {
                "version_date": row["version_date"] or None,
                "as_of": as_of,
                "source_url": source_url,
                "web_url": web_url,
                "retrieved_at": to_local(retrieved_at),
                "checksum": row["checksum"],
            },
            "freshness": freshness,
            "warnings": warnings,
        }

    # ════════════════════════════════════════════════════════════
    # 工具 4：search_clauses —— 關鍵字檢索條文
    # ════════════════════════════════════════════════════════════

    def search_clauses(
        self,
        query: str,
        source_id: str | None = None,
        scope: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return _invalid("query（關鍵字）不可以是空的。")
        limit = max(1, min(int(limit), 50))
        scope = (scope or "").strip() or None
        source_id = (source_id or "").strip() or None
        if source_id:
            src = SOURCES.get(source_id)
            if src is None:
                return self._unknown_source(source_id)
            targets = [src]
        else:
            targets = list(SOURCES.values())

        results: list[dict[str, Any]] = []
        searched = []
        for src in targets:
            if src.provider == "ecfr":
                outcome = self._search_ecfr(src, query, scope, limit)
            else:
                outcome = self._search_tw(src, query, limit)
            results.extend(outcome.pop("results"))
            searched.append(outcome)

        statuses = {outcome["status"] for outcome in searched}
        if statuses == {"ok"}:
            status = "ok"
        elif len(statuses) == 1:
            status = statuses.pop()
        else:
            status = "partial"
        failed = [o["source_id"] for o in searched if o["status"] != "ok"]
        if results:
            message = f"共回傳 {len(results)} 筆。摘要只供定位，要引用原文請用 get_clause 取全文。"
        else:
            message = f"沒有找到符合「{query}」的條文。{NO_GUESS}"
        if failed:
            message += f" 注意：以下法源的結果不完整或無法確認是否最新：{'、'.join(failed)}。"
        return {
            "status": status,
            "query": query,
            "total_returned": len(results),
            "results": results,
            "sources_searched": searched,
            "message": message,
        }

    def _search_ecfr(
        self, src: Source, query: str, part: str | None, limit: int
    ) -> dict[str, Any]:
        if part and not re.fullmatch(r"\d{1,4}", part):
            return {
                "source_id": src.id,
                "status": "invalid_input",
                "message": f"scope 看不懂：「{part}」。{src.scope_hint}。",
                "results": [],
            }
        try:
            status = ecfr.fetch_title_status(self.http)
            # eCFR 的搜尋沒有「限定 Part」的公開參數，所以指定 Part 時多抓一些再自己篩
            per_page = 1000 if part else min(limit + 20, 100)
            page = ecfr.search(self.http, query, status.up_to_date_as_of, per_page)
        except (SourceUnavailable, NotFound) as exc:
            self.store.record_check(src.id, False, None, str(exc))
            return {
                "source_id": src.id,
                "status": "source_unavailable",
                "message": f"連不上 {src.provider_name}，無法搜尋，也無法確認是否最新。",
                "error": str(exc),
                "results": [],
            }
        self.store.record_check(
            src.id, True, status.latest_amended_on, f"eCFR 資料更新至 {status.up_to_date_as_of}"
        )
        hits = [r for r in page.results if not part or r["part"] == part]
        retrieved_at = to_local(now_utc())
        results = [
            {
                "source_id": src.id,
                "clause_id": hit["section"],
                "citation": ecfr.citation(hit["section"]),
                "heading": hit["heading"],
                "context": f"Part {hit['part']}: {hit['part_heading']}",
                "excerpt": hit["excerpt"],
                "version_date": hit["starts_on"],
                "web_url": ecfr.web_url(hit["section"], status.up_to_date_as_of),
                "retrieved_at": retrieved_at,
            }
            for hit in hits[:limit]
        ]
        matched = len(hits) if part else page.total
        return {
            "source_id": src.id,
            "status": "ok",
            "matched": matched,
            "returned": len(results),
            "provenance": {
                "source_url": page.url,
                "retrieved_at": retrieved_at,
                "as_of": status.up_to_date_as_of,
            },
            "message": (
                f"eCFR 搜尋（{status.up_to_date_as_of} 當時有效的條文"
                + (f"，限 Part {part}" if part else "")
                + f"）：符合 {matched} 筆，回傳相關度最高的 {len(results)} 筆。"
            ),
            "results": results,
        }

    def _search_tw(self, src: Source, query: str, limit: int) -> dict[str, Any]:
        status = "ok"
        warnings: list[str] = []
        try:
            sync = self._sync_tw(src)
        except SourceUnavailable as exc:
            self.store.record_check(src.id, False, None, str(exc))
            if not any(self.store.version_dates(src.id)):
                return {
                    "source_id": src.id,
                    "status": "source_unavailable",
                    "message": f"連不上 {src.provider_name}，本機也沒有收錄 {src.name}，無法搜尋。",
                    "error": str(exc),
                    "results": [],
                }
            status = "unverified"
            warnings.append(f"連不上來源，以下是用本機舊資料搜尋的結果，無法確認是否最新（{exc}）。")
        else:
            if sync.lagging:
                status = "outdated"
                warnings.extend(sync.warnings)

        latest = next(d for d in self.store.version_dates(src.id) if d)
        keywords = [k.lower() for k in query.split()]
        hits = []
        for row in self.store.snapshot(src.id, latest):
            if row["clause_id"].startswith("_"):
                continue
            lowered = row["content"].lower()
            counts = [lowered.count(keyword) for keyword in keywords]
            if all(counts):  # 每個關鍵字都要出現
                hits.append((sum(counts), row))
        hits.sort(key=lambda hit: -hit[0])  # 出現次數多的排前面；同分維持條號順序
        results = [
            {
                "source_id": src.id,
                "clause_id": row["clause_id"],
                "citation": row["citation"],
                "heading": row["heading"],
                "context": src.name,
                "excerpt": _excerpt(row["content"], keywords[0]),
                "version_date": row["version_date"],
                "web_url": twlaw.article_url(src.pcode, row["clause_id"]),
                "retrieved_at": to_local(row["fetched_at"]),
            }
            for _, row in hits[:limit]
        ]
        return {
            "source_id": src.id,
            "status": status,
            "matched": len(hits),
            "returned": len(results),
            "provenance": {
                "source_url": twlaw.BULK_URLS[src.bulk_kind],
                "version_date": latest,
                "web_url": twlaw.page_url(src.pcode),
            },
            "warnings": warnings,
            "message": (
                f"{src.name}（{latest} 版）：符合 {len(hits)} 條，回傳 {len(results)} 條。"
                "比對方式是條文內容必須包含每一個關鍵字（以空白分隔）。"
            ),
            "results": results,
        }

    # ════════════════════════════════════════════════════════════
    # 共用：台灣法規同步、錯誤回報
    # ════════════════════════════════════════════════════════════

    def _sync_tw(self, src: Source, force: bool = False) -> _TwSync:
        """確認台灣法規有沒有新版；本機沒有或已落後時，下載整包資料補上新版本。

        force=False 時，如果幾分鐘內才成功確認過，就直接沿用那次確認的結果，不重複連線。
        """
        dates = [d for d in self.store.version_dates(src.id) if d]
        local_date = dates[0] if dates else None
        recent = self.store.last_check(src.id)
        if (
            not force
            and local_date
            and recent
            and recent["ok"]
            and recent["source_last_amended_on"]
            and _seconds_since(recent["checked_at"]) < TW_RECHECK_AFTER_SECONDS
        ):
            page_date = recent["source_last_amended_on"]
            lagging = local_date < page_date
            warnings = [_lag_warning(src, page_date, local_date)] if lagging else []
            return _TwSync(
                page_date, twlaw.page_url(src.pcode), local_date, None, lagging, recent, warnings
            )

        try:
            page_date, page_url = twlaw.fetch_page_modified_date(self.http, src.pcode)
        except NotFound as exc:
            raise SourceUnavailable(f"法規頁不存在：{exc}") from exc

        bulk_update_date = None
        warnings: list[str] = []
        if local_date is None or local_date < page_date:
            try:
                law = twlaw.fetch_law(self.http, src.bulk_kind, src.pcode)
                if law is None:
                    raise SourceUnavailable(f"官方整包資料裡找不到 {src.name}（{src.pcode}）")
            except (SourceUnavailable, NotFound) as exc:
                if local_date is None:
                    raise SourceUnavailable(str(exc)) from exc
                warnings.append(
                    f"官網顯示 {src.name} 已於 {page_date} 修正，但下載整包資料失敗（{exc}），"
                    f"本機仍是 {local_date} 的版本。"
                )
            else:
                bulk_update_date = law.bulk_update_date
                if law.modified_date not in dates:
                    self._store_tw_law(src, law)
                local_date = max(local_date or "", law.modified_date)
                if local_date < page_date:
                    warnings.append(
                        _lag_warning(src, page_date, local_date)
                        + f"（官方整包資料的更新日：{bulk_update_date or '不明'}）"
                    )
        lagging = local_date < page_date
        detail = f"官網修正日期 {page_date}；本機版本 {local_date}"
        if lagging:
            detail += "；尚未取得新版條文"
        check = self.store.record_check(src.id, True, page_date, detail)
        return _TwSync(page_date, page_url, local_date, bulk_update_date, lagging, check, warnings)

    def _store_tw_law(self, src: Source, law: twlaw.TwLaw) -> None:
        """把一個版本的整部法規逐條存進本機（只新增，不動舊版本）。"""
        for article in law.articles:
            self.store.add_version(
                src.id, article.no, twlaw.citation(src.name, article.no), article.heading,
                law.modified_date, article.content, law.source_url,
            )
        extras = ((HISTORY_ID, "沿革", law.histories), (EFFECTIVE_NOTE_ID, "生效內容註記", law.effective_note))
        for clause_id, label, content in extras:
            if content:
                self.store.add_version(
                    src.id, clause_id, f"{src.name} {label}", "", law.modified_date,
                    content, law.source_url,
                )

    def _unavailable(self, src: Source, exc: Exception) -> dict[str, Any]:
        """連不上來源：記下這次失敗的檢查，並明講無法確認是否最新。"""
        check = self.store.record_check(src.id, False, None, str(exc))
        last_ok = self.store.last_check(src.id, only_ok=True)
        return {
            "status": "source_unavailable",
            "source_id": src.id,
            "message": (
                f"連不上來源（{src.provider_name}），無法確認是否最新，"
                f"也無法列出改版內容。{NO_GUESS}"
            ),
            "error": str(exc),
            "last_successful_check_at": to_local(last_ok["checked_at"]) if last_ok else None,
            "check": _check_view(check),
        }

    def _unknown_source(self, source_id: str) -> dict[str, Any]:
        known = "、".join(SOURCES)
        return _invalid(f"沒有登錄代號為「{source_id}」的法源。已登錄的有：{known}。")


def _lag_warning(src: Source, page_date: str, local_date: str) -> str:
    return (
        f"官網顯示 {src.name} 已於 {page_date} 修正，但官方整包開放資料還是 {local_date} 的版本，"
        "本機尚未取得最新條文。"
    )


def _excerpt(content: str, keyword: str, before: int = 30, after: int = 70) -> str:
    """擷取關鍵字前後的一小段文字，方便判斷是不是要找的那一條。"""
    flat = content.replace("\n", " ")
    index = flat.lower().find(keyword)
    if index < 0:
        return flat[: before + after]
    start = max(0, index - before)
    end = min(len(flat), index + len(keyword) + after)
    return ("…" if start > 0 else "") + flat[start:end] + ("…" if end < len(flat) else "")
