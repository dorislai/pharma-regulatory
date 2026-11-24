"""MCP server 的外殼：把 service.py 的四個功能登記成 Claude 看得懂的「工具」。

MCP 的運作方式（白話版）：
1. Claude 啟動這支程式，雙方透過「標準輸入／輸出」（stdio）互傳 JSON 訊息。
2. Claude 先問「你有哪些工具？」，這支程式回覆下面四個工具的名稱、說明、參數。
   —— 每個函式的說明文字（docstring）就是 Claude 讀到的工具說明。
3. 對話中 Claude 判斷需要查法規時，送出「呼叫某工具＋參數」；
   這支程式執行固定的查詢程式，把結果（JSON）傳回去。

注意：因為標準輸出被拿來傳訊息，這支程式不可以用 print 印東西，
要記錄訊息一律寫到標準錯誤（stderr）。
"""

from __future__ import annotations

import logging
import sys
from functools import partial
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .config import db_path
from .http import HttpClient
from .service import NO_GUESS, RegulationService
from .store import Store

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # 連線紀錄由 http.py 自己記一份就好
logger = logging.getLogger("pharma_reg_mcp")

INSTRUCTIONS = """\
這個 server 提供製藥法規條文的查詢，資料全部由固定程式從官方公開來源抓取
（美國 eCFR Title 21、台灣全國法規資料庫），本身不做任何解讀或比對。

使用規則：
1. 回答法規內容時，條文文字必須來自 get_clause 回傳的 text，不可以憑記憶引用或改寫條文。
2. 引用時要附上出處：citation（條號）、provenance.version_date（版本日期）、
   provenance.web_url（來源網址）、provenance.retrieved_at（擷取時間）。
3. status 不是 ok 時，要把 message 與 warnings 如實告訴使用者：
   not_found 就說查不到；unverified、outdated、source_unavailable 就說無法確認是否最新。
4. 不確定法源代號或條號寫法時，先呼叫 list_sources。
"""

mcp = FastMCP("pharma-reg-mcp", instructions=INSTRUCTIONS)

# 四個工具都只是「查資料」，不會更動任何外部系統（只會在本機留下查詢與版本紀錄）。
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

_service: RegulationService | None = None


def get_service() -> RegulationService:
    """第一次用到時才建立（開資料庫、準備連線），之後重複使用同一個。"""
    global _service
    if _service is None:
        _service = RegulationService(Store(db_path()), HttpClient())
    return _service


async def _run(method: str, **kwargs: Any) -> dict[str, Any]:
    """在背景執行查詢（連網需要等待），並把任何意外錯誤轉成誠實的回報。"""
    try:
        function = getattr(get_service(), method)
        return await anyio.to_thread.run_sync(partial(function, **kwargs))
    except Exception as exc:  # 任何沒預料到的錯誤，都不能讓 Claude 以為「有查到」
        logger.exception("工具 %s 執行失敗", method)
        return {
            "status": "error",
            "message": (
                f"工具內部發生錯誤（{type(exc).__name__}: {exc}），沒有取得任何資料。{NO_GUESS}"
            ),
        }


@mcp.tool(annotations=READ_ONLY)
async def list_sources() -> dict[str, Any]:
    """列出已登錄的法源，以及各自的最後檢查時間與最後變更時間。

    不連網，只讀本機紀錄。回傳每個法源的 source_id（其他工具要用的代號）、
    條號寫法、last_checked_at（最後一次向來源確認的時間）、
    source_last_amended_on（來源公告的最後修訂日）。
    last_checked_at 為空代表還沒檢查過，請先呼叫 check_updates。
    """
    return await _run("list_sources")


@mcp.tool(annotations=READ_ONLY)
async def check_updates(
    source_id: Annotated[str, Field(description="法源代號，例如 us-21cfr（可用 list_sources 查）")],
    since: Annotated[str, Field(description="起始日期 YYYY-MM-DD，查這一天（含）之後的改版")],
    scope: Annotated[
        str | None,
        Field(description="（選填）縮小範圍。us-21cfr 可填 Part 編號如 211；台灣法規不適用"),
    ] = None,
    max_items: Annotated[int, Field(description="最多列出幾筆異動（1–500）")] = 50,
) -> dict[str, Any]:
    """即時向官方來源查詢：某法源自某日起有哪些條文改版。

    會連網，並把這次檢查記錄在本機。回傳 has_updates、異動清單（條號、版本日期、
    來源網址）與查詢出處。連不上來源時 status 為 source_unavailable，
    代表「無法確認是否最新」，不可當作「沒有改版」。
    台灣法規的官方開放資料只有現行版，所以改版以整部法規為單位回報，
    並附上官方沿革原文（amendment_note）說明修了哪些條。
    """
    return await _run(
        "check_updates", source_id=source_id, since=since, scope=scope, max_items=max_items
    )


@mcp.tool(annotations=READ_ONLY)
async def get_clause(
    source_id: Annotated[str, Field(description="法源代號，例如 us-21cfr、tw-pharmaceutical-affairs-act")],
    clause_id: Annotated[
        str, Field(description="條號。21 CFR 寫 section，如 211.22；台灣法規寫條號，如 57 或 6-1")
    ],
    as_of: Annotated[
        str | None,
        Field(description="（選填）YYYY-MM-DD，回查這一天當時有效的版本；不填就是最新版"),
    ] = None,
) -> dict[str, Any]:
    """取得某一條條文的原文（逐字），可指定日期回查當時的版本。

    回傳 text（原文）與 provenance（版本日期、來源網址、擷取時間、內容指紋）。
    status 為 not_found 表示查不到，此時沒有 text，請直接告訴使用者查不到；
    status 為 unverified 或 outdated 表示附上的是本機舊資料、無法確認是否最新，
    必須把 warnings 一併告訴使用者。
    21 CFR 可回查歷史版本；台灣法規的官方資料只有現行版，更早的日期會回 not_found。
    """
    return await _run("get_clause", source_id=source_id, clause_id=clause_id, as_of=as_of)


@mcp.tool(annotations=READ_ONLY)
async def search_clauses(
    query: Annotated[str, Field(description="關鍵字。21 CFR 用英文；台灣法規用中文，多個詞以空白分隔")],
    source_id: Annotated[
        str | None, Field(description="（選填）只搜尋這個法源；不填就搜尋全部已登錄的法源")
    ] = None,
    scope: Annotated[
        str | None, Field(description="（選填）us-21cfr 可填 Part 編號如 211，只看該 Part 的結果")
    ] = None,
    limit: Annotated[int, Field(description="每個法源最多回傳幾筆（1–50）")] = 10,
) -> dict[str, Any]:
    """用關鍵字檢索條文，找出「可能相關的是哪幾條」。

    每筆結果有條號、標題、摘要、版本日期、來源網址與擷取時間。
    摘要只是用來定位的片段，不是完整條文；要引用時請再用 get_clause 取得全文。
    沒有結果就是沒有找到，請如實回報。
    """
    return await _run(
        "search_clauses", query=query, source_id=source_id, scope=scope, limit=limit
    )


def main() -> None:
    """啟動 server，用 stdio 與 Claude 溝通。"""
    mcp.run()


if __name__ == "__main__":
    main()
