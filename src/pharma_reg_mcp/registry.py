"""已登錄的法源清單。

想多收一部法規，就在這裡多加一筆；沒登錄的法源，工具一律回答「未登錄」。
v0.1 只接兩個有官方 API 的資料提供者：
- 美國 eCFR（Title 21）
- 台灣全國法規資料庫 OpenAPI（先收藥事法、藥物優良製造準則兩部）
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    id: str               # 工具參數用的代號
    name: str             # 給人看的名稱
    jurisdiction: str     # 管轄地區
    language: str         # 條文語言
    provider: str         # 走哪個轉接頭："ecfr" 或 "twlaw"
    provider_name: str    # 資料提供者名稱
    homepage: str         # 人可以直接打開看的官方頁面
    clause_id_hint: str   # 條號怎麼寫
    scope_hint: str       # scope 參數怎麼用
    pcode: str = ""       # （台灣）法規代碼
    bulk_kind: str = ""   # （台灣）在哪一包開放資料裡："Law" 法律 / "Order" 命令


SOURCES: dict[str, Source] = {
    source.id: source
    for source in (
        Source(
            id="us-21cfr",
            name="美國聯邦法規 21 CFR（Title 21: Food and Drugs）",
            jurisdiction="US",
            language="en",
            provider="ecfr",
            provider_name="eCFR（美國聯邦法規電子版）官方 API",
            homepage="https://www.ecfr.gov/current/title-21",
            clause_id_hint="條號（section），例如 211.22",
            scope_hint="可填 Part 編號縮小範圍，例如 211",
        ),
        Source(
            id="tw-pharmaceutical-affairs-act",
            name="藥事法",
            jurisdiction="TW",
            language="zh-Hant",
            provider="twlaw",
            provider_name="全國法規資料庫 OpenAPI",
            homepage="https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0030001",
            clause_id_hint="條號，例如 57 或 6-1（即第 6 條之 1）",
            scope_hint="不適用（整部法規就是一個範圍）",
            pcode="L0030001",
            bulk_kind="Law",
        ),
        Source(
            id="tw-drug-gmp-regulations",
            name="藥物優良製造準則",
            jurisdiction="TW",
            language="zh-Hant",
            provider="twlaw",
            provider_name="全國法規資料庫 OpenAPI",
            homepage="https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0030073",
            clause_id_hint="條號，例如 3",
            scope_hint="不適用（整部法規就是一個範圍）",
            pcode="L0030073",
            bulk_kind="Order",
        ),
    )
}
