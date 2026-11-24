"""測試共用的工具：一個「假的官方網站」和幾個做假資料的小函式。

單元測試完全不連網路。FakeHttp 讓我們可以指定每個網址要回什麼——
正常資料、查不到（NotFound）、或連不上（SourceUnavailable）——
藉此驗證程式在各種情況下都照規則回報。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from pharma_reg_mcp.service import RegulationService
from pharma_reg_mcp.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


class FakeHttp:
    """假的對外連線：依「網址包含哪段文字」決定回什麼。後加入的規則優先。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, object]] = []
        self.calls: list[str] = []

    def add(self, url_fragment: str, response: object) -> None:
        self.routes.insert(0, (url_fragment, response))

    def get(self, url: str, timeout: float | None = None) -> bytes:
        self.calls.append(url)
        for fragment, response in self.routes:
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response  # type: ignore[return-value]
        raise AssertionError(f"測試沒有為這個網址準備假回應：{url}")


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


@pytest.fixture
def service(store: Store, http: FakeHttp) -> RegulationService:
    return RegulationService(store, http)  # type: ignore[arg-type]


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ── eCFR 假資料 ─────────────────────────────────────────────────


def ecfr_titles(amended: str = "2026-11-12", current: str = "2026-11-20") -> bytes:
    return json.dumps(
        {
            "titles": [
                {"number": 20, "name": "Employees' Benefits", "latest_amended_on": "2026-01-01",
                 "up_to_date_as_of": current},
                {"number": 21, "name": "Food and Drugs", "latest_amended_on": amended,
                 "latest_issue_date": amended, "up_to_date_as_of": current, "reserved": False},
            ],
            "meta": {"date": current, "import_in_progress": False},
        }
    ).encode()


def ecfr_version(identifier: str, version_date: str, issue_date: str | None = None, **extra) -> dict:
    return {
        "date": version_date,
        "amendment_date": version_date,
        "issue_date": issue_date or version_date,
        "identifier": identifier,
        "name": f"§ {identifier}   Test heading.",
        "part": identifier.split(".")[0],
        "substantive": True,
        "removed": False,
        "title": "21",
        "type": "section",
        **extra,
    }


def ecfr_versions(entries: list[dict], total_pages: int = 1) -> bytes:
    meta = {"title": "21", "result_count": str(len(entries)), "total_pages": str(total_pages)}
    return json.dumps({"content_versions": entries, "meta": meta}).encode()


# 21 CFR 211.52 的真實版本紀錄（2025-05-27 有一次修訂）
VERSIONS_211_52 = [
    ecfr_version("211.52", "2016-11-29", "2016-12-31"),
    ecfr_version("211.52", "2016-11-29", "2025-03-12", substantive=False),
    ecfr_version("211.52", "2025-05-27"),
]


def add_ecfr_211_52(http: FakeHttp) -> None:
    """準備好查詢 21 CFR 211.52（現行版與 2025-04-01 舊版）所需的全部假回應。"""
    http.add("titles.json", ecfr_titles())
    http.add("versions/title-21.json", ecfr_versions(VERSIONS_211_52))
    http.add("full/2026-11-20/title-21.xml", fixture_bytes("ecfr_211_52_current.xml"))
    http.add("full/2025-04-01/title-21.xml", fixture_bytes("ecfr_211_52_2025-04-01.xml"))


# ── 台灣全國法規資料庫假資料 ────────────────────────────────────

ACT_ARTICLES = [
    ("C", "", "   第 一 章 總則"),
    ("A", "第 4 條", "本法所稱藥物，係指藥品及醫療器材。"),
    ("A", "第 5 條", "本法所稱試驗用藥物，係指醫療效能及安全尚未經證實，專供動物毒性藥理評估或臨床試驗用之藥物。"),
    ("A", "第 6-1 條", "經中央衛生主管機關公告類別之藥品，其販賣業者或製造業者，應依其產業模式建立藥品來源及流向之追溯或追蹤系統。"),
    ("C", "", "   第 五 章 藥物之販賣及製造"),
    ("A", "第 61 條", "（刪除）"),
]

ACT_HISTORIES = (
    "1.中華民國五十九年八月十七日總統令制定公布全文 90 條\r\n"
    "20. 中華民國一百十五年三月四日總統華總一義字第 11500017931  號令\r\n"
    "    修正公布第 27-2、48-2、80、94、96-1、106  條條文；增訂第 27-\r\n"
    "    3 條條文"
)


def tw_law(
    pcode: str = "L0030001",
    name: str = "藥事法",
    modified: str = "20260304",
    articles: list[tuple[str, str, str]] | None = None,
    histories: str = ACT_HISTORIES,
    effective_note: str = "",
) -> dict:
    """一部法規在整包資料裡的樣子（欄位順序與官方資料相同）。"""
    return {
        "LawLevel": "法律",
        "LawName": name,
        "LawURL": f"https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode={pcode}",
        "LawCategory": "行政＞衛生福利部＞食品藥物管理目",
        "LawModifiedDate": modified,
        "LawEffectiveDate": "",
        "LawEffectiveNote": effective_note,
        "LawAbandonNote": "",
        "LawHasEngVersion": "Y",
        "EngLawName": "Pharmaceutical Affairs Act",
        "LawAttachements": [],
        "LawHistories": histories,
        "LawForeword": "",
        "LawArticles": [
            {"ArticleType": kind, "ArticleNo": number, "ArticleContent": content}
            for kind, number, content in (articles or ACT_ARTICLES)
        ],
    }


OTHER_LAW = tw_law(
    pcode="A0000001", name="測試用的另一部法規", modified="20200101",
    articles=[("A", "第 1 條", "這是排在前面的另一部法規，用來確認程式不會拿錯。")],
    histories="",
)


def tw_bulk_zip(laws: list[dict], update_date: str = "2026/11/13 上午 12:00:00") -> bytes:
    """做一個和官方整包資料同樣格式的 ZIP（JSON 有縮排、CRLF 換行、開頭有 BOM）。"""
    text = json.dumps({"UpdateDate": update_date, "Laws": laws}, ensure_ascii=False, indent=2)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ChLaw.json", ("﻿" + text.replace("\n", "\r\n")).encode("utf-8"))
        archive.writestr("schema.csv", "name,title\r\nLawLevel,法規位階\r\n")
    return buffer.getvalue()


def tw_page(roc_year: int = 115, month: int = 3, day: int = 4, label: str = "修正日期") -> bytes:
    """法規頁面中顯示日期的那一小段 HTML。"""
    return (
        "<table><tr>\r\n\t<th>法規名稱：</th>\r\n\t<td>藥事法</td>\r\n</tr>\r\n<tr>\r\n"
        f"\t<th>{label}：</th>\r\n\t<td>民國 {roc_year} 年 {month:02d} 月 {day:02d} 日 </td>\r\n"
        "</tr></table>"
    ).encode("utf-8")


def add_tw_act(http: FakeHttp, law: dict | None = None, page: bytes | None = None) -> None:
    """準備好查詢藥事法所需的假回應（法規頁 + 整包資料）。"""
    http.add("LawAll.aspx?pcode=L0030001", page or tw_page())
    http.add("api/Ch/Law/JSON", tw_bulk_zip([OTHER_LAW, law or tw_law()]))
