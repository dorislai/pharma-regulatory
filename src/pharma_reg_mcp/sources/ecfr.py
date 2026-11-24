"""美國 eCFR（Title 21）轉接頭。

只使用官方文件有記載的端點與參數
（https://www.ecfr.gov/developers/documentation/api/v1）：

- titles.json            這個 Title 最後修訂日、資料更新到哪一天
- versions/title-21.json 各條文的版本清單（可依發布日期篩選）
- full/{日期}/title-21.xml 某一天當時的條文原文（XML）
- search/v1/results      關鍵字搜尋

這個檔案裡全部是固定規則的程式，不使用任何語言模型。
"""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from ..http import HttpClient, SourceUnavailable

BASE_URL = "https://www.ecfr.gov"
TITLE = 21

# 搜尋端點沒有「限定 Title」的公開參數，但有「限定主管機關」的 agency_slugs[]。
# 這三個機關合起來剛好涵蓋 Title 21 的全部三個 Chapter（I: FDA、II: DEA、III: ONDCP）。
AGENCY_SLUGS = (
    "food-and-drug-administration",
    "drug-enforcement-administration",
    "office-of-national-drug-control-policy",
)

MAX_VERSION_PAGES = 10  # 版本清單一頁 1000 筆，最多翻 10 頁，避免無止境地抓

_SECTION_RE = re.compile(r"(?<![\d.])(\d{1,4}\.\d+[a-z]?)(?![\d])")


# ── 條號與網址 ──────────────────────────────────────────────────


def normalize_section(raw: str) -> str | None:
    """把各種寫法整理成標準條號。'21 CFR 211.22(a)'、'§ 211.22' → '211.22'。"""
    text = re.sub(r"(?i)\b21\s*CFR\b", " ", raw or "")
    match = _SECTION_RE.search(text)
    return match.group(1) if match else None


def citation(section: str) -> str:
    return f"{TITLE} CFR {section}"


def web_url(section: str, date: str) -> str:
    """人可以直接打開、看到「那一天的那一條」的官方網頁。"""
    return f"{BASE_URL}/on/{date}/title-{TITLE}/section-{section}"


def _url(path: str, pairs: list[tuple[str, Any]] | None = None) -> str:
    query = urlencode([(k, v) for k, v in (pairs or []) if v is not None], safe="[]")
    return f"{BASE_URL}{path}" + (f"?{query}" if query else "")


def _json(raw: bytes, url: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise SourceUnavailable(f"來源回應不是預期的 JSON 格式：{url}") from exc
    if not isinstance(payload, dict):
        raise SourceUnavailable(f"來源回應不是預期的 JSON 格式：{url}")
    return payload


# ── 1. Title 狀態：用來判斷「來源最近有沒有改版」 ────────────────


@dataclass
class TitleStatus:
    latest_amended_on: str   # 這個 Title 最後一次修訂的日期
    up_to_date_as_of: str    # eCFR 的資料更新到哪一天
    import_in_progress: bool  # eCFR 是否正在匯入新資料
    url: str


def fetch_title_status(http: HttpClient) -> TitleStatus:
    url = _url("/api/versioner/v1/titles.json")
    payload = _json(http.get(url), url)
    for title in payload.get("titles", []):
        if title.get("number") == TITLE:
            amended = title.get("latest_amended_on")
            current = title.get("up_to_date_as_of")
            if not amended or not current:
                break
            return TitleStatus(
                latest_amended_on=amended,
                up_to_date_as_of=current,
                import_in_progress=bool((payload.get("meta") or {}).get("import_in_progress")),
                url=url,
            )
    raise SourceUnavailable(f"來源回應中找不到 Title {TITLE} 的狀態：{url}")


# ── 2. 版本清單：哪些條文在什麼時候改過 ─────────────────────────


@dataclass
class VersionList:
    entries: list[dict[str, Any]]
    url: str         # 查詢用的網址（第一頁）
    total: int       # 來源說總共有幾筆
    truncated: bool  # 筆數太多、沒有全部抓完


def fetch_versions(
    http: HttpClient,
    *,
    since: str | None = None,
    until: str | None = None,
    part: str | None = None,
    section: str | None = None,
) -> VersionList:
    """取得版本清單。since / until 以「發布日期（issue date）」篩選。"""
    pairs: list[tuple[str, Any]] = [
        ("part", part),
        ("section", section),
        ("issue_date[gte]", since),
        ("issue_date[lte]", until),
    ]
    path = f"/api/versioner/v1/versions/title-{TITLE}.json"
    first_url = _url(path, pairs)
    entries: list[dict[str, Any]] = []
    total = 0
    total_pages = 1
    page = 1
    while page <= total_pages and page <= MAX_VERSION_PAGES:
        url = first_url if page == 1 else _url(path, pairs + [("page", page)])
        payload = _json(http.get(url), url)
        entries.extend(payload.get("content_versions") or [])
        meta = payload.get("meta") or {}
        total = int(meta.get("result_count") or len(entries))
        total_pages = int(meta.get("total_pages") or 1)
        page += 1
    return VersionList(entries, first_url, total, truncated=total_pages > MAX_VERSION_PAGES)


def pick_version(entries: list[dict[str, Any]], as_of: str) -> dict[str, Any] | None:
    """從版本清單挑出「as_of 那一天有效的版本」：版本日期不晚於 as_of 的最新一筆。"""
    candidates = [e for e in entries if e.get("date") and e["date"] <= as_of]
    if not candidates:
        return None
    return max(candidates, key=lambda e: (e["date"], e.get("issue_date") or ""))


# ── 3. 條文原文 ─────────────────────────────────────────────────


@dataclass
class SectionText:
    heading: str
    text: str
    url: str


def section_url(section: str, date: str) -> str:
    part = section.split(".")[0]
    return _url(
        f"/api/versioner/v1/full/{date}/title-{TITLE}.xml",
        [("part", part), ("section", section)],
    )


def fetch_section(http: HttpClient, section: str, date: str) -> SectionText:
    """取得某條在某一天當時的原文。來源說沒有時，http 會丟出 NotFound。"""
    url = section_url(section, date)
    heading, text = parse_section_xml(http.get(url), section)
    return SectionText(heading, text, url)


def parse_section_xml(raw: bytes, expected_section: str) -> tuple[str, str]:
    """把 eCFR 的 XML 轉成（標題, 內文）。回傳的不是指定條文就視為失敗，絕不將錯就錯。"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceUnavailable("來源回傳的 XML 無法解析") from exc
    if root.tag != "DIV8" or root.get("N") != expected_section:
        raise SourceUnavailable(
            f"來源回傳的不是所要求的條文（要求 {expected_section}，"
            f"收到 {root.tag} {root.get('N')}）"
        )
    heading = ""
    blocks: list[str] = []
    for child in root:
        if child.tag == "HEAD" and not heading:
            heading = _clean("".join(child.itertext()))
            continue
        block = _block_text(child)
        if block:
            blocks.append(block)
    return heading, "\n".join(blocks)


def _block_text(element: ET.Element) -> str:
    """一個段落的文字。遇到表格時，一列一行、儲存格之間用「|」分隔。"""
    rows = list(element.iter("TR"))
    if not rows:
        return _clean("".join(element.itertext()))
    lines = []
    for row in rows:
        cells = [_clean("".join(cell.itertext())) for cell in row if cell.tag in ("TH", "TD")]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ── 4. 關鍵字搜尋 ───────────────────────────────────────────────


@dataclass
class SearchPage:
    results: list[dict[str, Any]]
    total: int
    url: str


def search(http: HttpClient, query: str, date: str, per_page: int) -> SearchPage:
    """搜尋 Title 21 在指定日期當時有效的條文。"""
    pairs: list[tuple[str, Any]] = [("query", query)]
    pairs += [("agency_slugs[]", slug) for slug in AGENCY_SLUGS]
    pairs += [("date", date), ("per_page", per_page), ("page", 1)]
    url = _url("/api/search/v1/results", pairs)
    payload = _json(http.get(url), url)
    results = []
    for item in payload.get("results") or []:
        hierarchy = item.get("hierarchy") or {}
        headings = item.get("headings") or {}
        # 只收 Title 21 的「條文」（附錄等其他類型 v0.1 先不處理）
        if str(hierarchy.get("title")) != str(TITLE) or item.get("type") != "Section":
            continue
        if not hierarchy.get("section"):
            continue
        results.append(
            {
                "section": hierarchy["section"],
                "part": hierarchy.get("part"),
                "heading": _strip_tags(headings.get("section") or ""),
                "part_heading": _strip_tags(headings.get("part") or ""),
                "excerpt": _strip_tags(item.get("full_text_excerpt") or ""),
                "starts_on": item.get("starts_on"),
            }
        )
    total = int((payload.get("meta") or {}).get("total_count") or 0)
    return SearchPage(results, total, url)


def _strip_tags(text: str) -> str:
    """搜尋結果的摘要裡夾了 <strong> 等標記，拿掉只留文字。"""
    return _clean(html.unescape(re.sub(r"<[^>]+>", "", text)))
