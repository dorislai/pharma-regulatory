"""端到端冒煙測試：像 Claude 一樣，透過 MCP 協定啟動 server，實際呼叫 4 個工具。

和單元測試的差別：
- 單元測試（tests/）不連網路，用假資料檢查每一塊邏輯。
- 這支測試會真的連上 eCFR 和全國法規資料庫，確認整條線是通的。

執行方式（在專案資料夾裡）：
    uv run python scripts/smoke_test.py

每次執行都用全新的暫存資料夾當本機資料庫，所以也會測到「第一次使用」的流程
（台灣法規要下載官方整包資料，約需 10–30 秒）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {"list_sources", "check_updates", "get_clause", "search_clauses"}
US = "us-21cfr"
ACT = "tw-pharmaceutical-affairs-act"
GMP = "tw-drug-gmp-regulations"
RETRY_WAIT_SECONDS = 20  # 官方來源暫時連不上時，等這麼久再試一次

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    """記錄一項檢查的結果。"""
    print(f"   {'✅' if condition else '❌'} {description}")
    if not condition:
        failures.append(description)


def title(text: str) -> None:
    print(f"\n{'─' * 70}\n{text}\n{'─' * 70}")


def shorten(text: str, limit: int = 110) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def show_provenance(result: dict[str, Any]) -> None:
    if "text" not in result:
        print(f"   （沒有條文內容）{result.get('message', '')}")
        return
    p = result["provenance"]
    print(f"   條號：{result['citation']}")
    print(f"   版本日期：{p['version_date']}（查詢基準日 {p['as_of']}）")
    print(f"   來源網址：{p['source_url']}")
    print(f"   官方網頁：{p['web_url']}")
    print(f"   擷取時間：{p['retrieved_at']}")
    print(f"   內容指紋：{p['checksum'][:23]}…")


def has_provenance(result: dict[str, Any]) -> bool:
    p = result.get("provenance", {})
    return all(p.get(k) for k in ("version_date", "source_url", "web_url", "retrieved_at", "checksum"))


async def call(session: ClientSession, name: str, **arguments: Any) -> dict[str, Any]:
    """透過 MCP 呼叫一個工具，回傳它給的 JSON。

    官方網站偶爾會暫時出錯。遇到「連不上來源」時等一下再試一次，
    兩次的結果都會印出來，不會把失敗藏起來。
    """
    payload = await call_once(session, name, arguments)
    if payload["status"] == "source_unavailable":
        reason = payload.get("error") or payload.get("message")
        print(f"   ⏳ 官方來源暫時連不上（{reason}）")
        print(f"      server 已如實回報，沒有給任何內容。{RETRY_WAIT_SECONDS} 秒後重試一次…")
        await asyncio.sleep(RETRY_WAIT_SECONDS)
        payload = await call_once(session, name, arguments)
    return payload


async def call_once(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    result = await session.call_tool(name, arguments)
    elapsed = time.monotonic() - started
    shown = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
    print(f"\n▶ {name}({shown})  [{elapsed:.1f} 秒]")
    if result.isError:
        raise RuntimeError(f"工具 {name} 回報協定層錯誤")
    payload = json.loads(result.content[0].text)
    print(f"   status = {payload['status']}")
    return payload


async def run(home: Path) -> None:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pharma_reg_mcp.server"],
        env={**os.environ, "PHARMA_REG_MCP_HOME": str(home)},
    )
    with open(home / "server.log", "w", encoding="utf-8") as server_log:
        async with stdio_client(server, errlog=server_log) as (read, write):
            async with ClientSession(read, write) as session:
                await exercise(session)


async def exercise(session: ClientSession) -> None:
    title("步驟 0｜連線：啟動 server 並「握手」")
    init = await session.initialize()
    print(f"   server 名稱：{init.serverInfo.name}（MCP 協定版本 {init.protocolVersion}）")
    tools = (await session.list_tools()).tools
    for tool in tools:
        print(f"   工具：{tool.name} — {(tool.description or '').strip().splitlines()[0]}")
    check({t.name for t in tools} == EXPECTED_TOOLS, "server 提供的正好是 4 個工具")

    title("步驟 1｜list_sources：一開始還沒檢查過任何法源")
    listed = await call(session, "list_sources")
    for src in listed["sources"]:
        print(f"   {src['source_id']}：{src['name']}｜最後檢查 {src['last_checked_at']}")
    check(len(listed["sources"]) == 3, "登錄了 3 個法源（21 CFR、藥事法、藥物優良製造準則）")
    check(all(s["last_checked_at"] is None for s in listed["sources"]), "全新資料庫：都還沒檢查過")

    title("步驟 2｜check_updates：21 CFR Part 211 自 2025-01-01 起改了哪些條")
    updates = await call(session, "check_updates", source_id=US, since="2025-01-01", scope="211")
    print(f"   {updates['message']}")
    for item in updates.get("items", []):
        mark = "" if item["substantive"] else "（非實質變更）"
        print(f"   - {item['citation']}｜版本日期 {item['version_date']}｜{shorten(item['name'], 50)}{mark}")
    if updates["status"] == "ok":
        print(f"   來源網址：{updates['provenance']['source_url']}")
        print(f"   擷取時間：{updates['provenance']['retrieved_at']}")
    check(updates["status"] == "ok" and updates.get("has_updates") is True, "查到改版紀錄")
    check(any(i["clause_id"] == "211.52" for i in updates.get("items", [])), "清單裡有 211.52（2025-05-27 修訂）")

    title("步驟 3｜get_clause：取得 21 CFR 211.52 的現行原文")
    current = await call(session, "get_clause", source_id=US, clause_id="211.52")
    if "text" in current:
        print(f"   標題：{current['heading']}")
        print(f"   原文：{current['text']}")
    show_provenance(current)
    check(current["status"] == "ok" and bool(current.get("text")), "拿到原文")
    check(has_provenance(current), "出處齊全（版本日期、來源網址、擷取時間、內容指紋）")
    check(current.get("freshness", {}).get("confirmed") is True, "已向來源確認是最新版")

    title("步驟 4｜get_clause + as_of：回查同一條在 2025-04-01 當時的版本")
    past = await call(session, "get_clause", source_id=US, clause_id="211.52", as_of="2025-04-01")
    if "text" in past:
        print(f"   原文：{past['text']}")
    show_provenance(past)
    check(past["status"] == "ok", "拿到歷史版本")
    if has_provenance(past) and has_provenance(current):
        check(
            past["provenance"]["version_date"] < current["provenance"]["version_date"],
            f"版本日期不同（當時 {past['provenance']['version_date']} → 現行 {current['provenance']['version_date']}）",
        )
        check(
            past["provenance"]["checksum"] != current["provenance"]["checksum"],
            "內容指紋不同：兩個版本的文字確實有差異",
        )
    else:
        check(False, "兩個版本都要拿到，才能比較版本日期與內容指紋")

    title("步驟 5｜get_clause：查一條不存在的條文，應該明講查不到")
    missing = await call(session, "get_clause", source_id=US, clause_id="211.9999")
    print(f"   {missing.get('message', '')}")
    check(missing["status"] == "not_found", "回報 not_found")
    check("text" not in missing, "沒有附上任何條文內容")

    title("步驟 6｜check_updates：藥事法自 2025-01-01 起有沒有改版")
    tw_updates = await call(session, "check_updates", source_id=ACT, since="2025-01-01")
    print(f"   {tw_updates['message']}")
    tw_items = tw_updates.get("items", [])
    for item in tw_items:
        print(f"   - {item['citation']}｜版本日期 {item['version_date']}")
        print(f"     官方沿革：{item.get('amendment_note')}")
    if tw_updates["status"] == "ok":
        print(f"   逐條明細：{tw_updates['clause_level_detail']}")
        print(f"   來源網址：{tw_updates['provenance']['source_url']}")
        print(f"   整包資料更新日：{tw_updates['provenance']['bulk_data_updated_on']}")
    check(tw_updates["status"] == "ok", "查詢成功")
    check(bool(tw_items) and bool(tw_items[0].get("amendment_note")), "查到改版，並附官方沿革原文")

    title("步驟 7｜get_clause：藥事法第 57 條、藥物優良製造準則第 3 條")
    article_57 = await call(session, "get_clause", source_id=ACT, clause_id="57")
    if "text" in article_57:
        print(f"   所在章節：{article_57['heading']}")
        print(f"   原文：{shorten(article_57['text'], 200)}")
    show_provenance(article_57)
    for warning in article_57.get("warnings", []):
        print(f"   ⚠ {warning}")
    if article_57.get("law_effective_note"):
        print(f"   生效內容註記：{article_57['law_effective_note']}")
    check(article_57["status"] == "ok" and has_provenance(article_57), "藥事法第 57 條：原文與出處齊全")

    gmp_3 = await call(session, "get_clause", source_id=GMP, clause_id="第 3 條")
    if "text" in gmp_3:
        print(f"   所在章節：{gmp_3['heading']}")
        print(f"   原文：{shorten(gmp_3['text'], 200)}")
    show_provenance(gmp_3)
    check(gmp_3["status"] == "ok" and has_provenance(gmp_3), "藥物優良製造準則第 3 條：原文與出處齊全")

    title("步驟 8｜get_clause + as_of：台灣法規只有現行版，回查舊日期應該明講查不到")
    too_early = await call(session, "get_clause", source_id=ACT, clause_id="57", as_of="2020-01-01")
    print(f"   {too_early.get('message', '')}")
    check(too_early["status"] == "not_found" and "text" not in too_early, "回報 not_found，沒有拿現行版充數")

    title("步驟 9｜search_clauses：關鍵字檢索")
    found = await call(
        session, "search_clauses", query="quality control unit", source_id=US, scope="211", limit=3
    )
    for hit in found["results"]:
        print(f"   - {hit['citation']}｜{hit['heading']}｜版本日期 {hit['version_date']}")
        print(f"     摘要：{shorten(hit['excerpt'])}")
        print(f"     來源：{hit['web_url']}（擷取 {hit['retrieved_at']}）")
    check(found["status"] == "ok" and any(h["clause_id"] == "211.22" for h in found["results"]),
          "英文關鍵字找到 21 CFR 211.22")

    found_tw = await call(session, "search_clauses", query="確效", source_id=GMP, limit=3)
    for hit in found_tw["results"]:
        print(f"   - {hit['citation']}｜版本日期 {hit['version_date']}")
        print(f"     摘要：{shorten(hit['excerpt'])}")
        print(f"     來源：{hit['web_url']}（擷取 {hit['retrieved_at']}）")
    if found_tw["status"] != "ok":
        print(f"   {found_tw['message']}")
    check(found_tw["status"] == "ok" and len(found_tw["results"]) > 0, "中文關鍵字在藥物優良製造準則找到條文")

    nothing = await call(session, "search_clauses", query="zzqx-no-such-term", source_id=ACT)
    print(f"   {nothing['message']}")
    check(nothing["results"] == [] and "沒有找到" in nothing["message"], "找不到時明講沒有找到")

    title("步驟 10｜list_sources：現在每個法源都有檢查紀錄了")
    listed = await call(session, "list_sources")
    for src in listed["sources"]:
        print(
            f"   {src['source_id']}\n"
            f"     最後檢查：{src['last_checked_at']}（成功：{src['last_check_ok']}）\n"
            f"     來源最後修訂日：{src['source_last_amended_on']}\n"
            f"     本機紀錄：{src['checks_recorded']} 次檢查、{src['versions_stored']} 筆條文版本"
        )
    check(all(s["last_checked_at"] and s["last_check_ok"] for s in listed["sources"]), "三個法源都有成功的檢查紀錄")
    check(all(s["source_last_amended_on"] for s in listed["sources"]), "都記下了來源的最後修訂日")


def inspect_database(home: Path) -> None:
    title("步驟 11｜直接打開本機 SQLite 資料庫，確認紀錄真的存下來了")
    conn = sqlite3.connect(home / "regulations.db")
    try:
        checks = conn.execute("SELECT COUNT(*), SUM(ok) FROM checks").fetchone()
        versions = conn.execute("SELECT COUNT(*) FROM clause_versions").fetchone()[0]
        us_versions = conn.execute(
            "SELECT clause_id, version_date, substr(checksum, 1, 19), fetched_at "
            "FROM clause_versions WHERE source_id = ? ORDER BY version_date",
            (US,),
        ).fetchall()
    finally:
        conn.close()
    print(f"   checks 表：{checks[0]} 筆檢查紀錄（其中 {checks[1]} 筆成功）")
    print(f"   clause_versions 表：{versions} 筆條文版本")
    for clause_id, version_date, checksum, fetched_at in us_versions:
        print(f"   - 21 CFR {clause_id}｜版本 {version_date}｜{checksum}…｜擷取於 {fetched_at}（UTC）")
    check(checks[0] > 0 and versions > 0, "檢查紀錄與條文版本都已寫入資料庫")
    check(len(us_versions) == 2, "21 CFR 211.52 的新舊兩個版本並存，沒有互相覆蓋")

    requests = [line for line in (home / "server.log").read_text(encoding="utf-8").splitlines() if "GET http" in line]
    print(f"\n   這次測試中，server 一共向官方來源發出 {len(requests)} 次請求。")
    print(f"   完整的連線紀錄：{home / 'server.log'}")


def describe_error(exc: BaseException) -> str:
    """把錯誤整理成一句話（非同步程式的錯誤可能是好幾個包在一起）。"""
    inner = getattr(exc, "exceptions", None)
    if inner:
        return "；".join(describe_error(e) for e in inner)
    return f"{type(exc).__name__}: {exc}"


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="pharma-reg-mcp-smoke-"))
    print(f"本次測試使用的暫存資料夾：{home}")
    started = time.monotonic()
    try:
        asyncio.run(run(home))
        inspect_database(home)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # 也涵蓋非同步程式包起來的「一組錯誤」
        failures.append(f"測試中斷：{describe_error(exc)}")
    title("結果")
    print(f"   總耗時 {time.monotonic() - started:.0f} 秒")
    if failures:
        print(f"   ❌ 有 {len(failures)} 項檢查沒通過：")
        for failure in failures:
            print(f"      - {failure}")
        return 1
    print("   ✅ 全部檢查通過：4 個工具都能透過 MCP 取得真實法規資料。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
