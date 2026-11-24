"""台灣全國法規資料庫轉接頭。

官方 OpenAPI（說明：https://law.moj.gov.tw/api/swagger/docs/v1）的特性：
- 只能「整包下載」：一次給全部法律（或全部命令）的現行條文，包成 ZIP。
- 只有現行版，沒有歷史版本；而且整包資料的更新會比官網慢幾天。

所以這裡用兩個訊號互相對照：
1. 個別法規頁上的「修正日期」 → 很快、很小，用來判斷官方是否已有新版。
2. 整包資料                   → 條文原文的來源；只有在本機沒有或已落後時才下載。

這個檔案裡全部是固定規則的程式，不使用任何語言模型。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from ..config import BULK_TIMEOUT_SECONDS
from ..http import HttpClient, SourceUnavailable

BASE_URL = "https://law.moj.gov.tw"
BULK_URLS = {
    "Law": f"{BASE_URL}/api/Ch/Law/JSON",      # 中文法律
    "Order": f"{BASE_URL}/api/Ch/Order/JSON",  # 中文命令
}
MAX_BULK_JSON_BYTES = 600 * 1024 * 1024  # 解壓縮後超過 600MB 就不處理，保護電腦記憶體

_ARTICLE_FULL_RE = re.compile(r"第\s*(\d+)\s*(?:-\s*(\d+))?\s*條(?:\s*之\s*(\d+))?")
_ARTICLE_BARE_RE = re.compile(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*條?\s*")
_PAGE_DATE_RE = (
    r"{label}：\s*</th>\s*<td[^>]*>\s*民國\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日"
)


# ── 條號與網址 ──────────────────────────────────────────────────


def normalize_article_no(raw: str) -> str | None:
    """把各種寫法整理成標準條號。'第 6-1 條'、'第6條之1'、'6-1' → '6-1'。"""
    text = raw or ""
    match = _ARTICLE_FULL_RE.search(text) or _ARTICLE_BARE_RE.fullmatch(text)
    if not match:
        return None
    main = int(match.group(1))
    sub = next((g for g in match.groups()[1:] if g), None)
    return f"{main}-{int(sub)}" if sub else str(main)


def citation(law_name: str, article_no: str) -> str:
    return f"{law_name} 第 {article_no} 條"


def page_url(pcode: str) -> str:
    return f"{BASE_URL}/LawClass/LawAll.aspx?pcode={pcode}"


def article_url(pcode: str, article_no: str) -> str:
    """人可以直接打開、看到這一條的官方網頁。"""
    return f"{BASE_URL}/LawClass/LawSingle.aspx?pcode={pcode}&flno={article_no}"


def roc_to_iso(year: int, month: int, day: int) -> str:
    """民國年月日 → 西元 YYYY-MM-DD（民國年 + 1911）。"""
    return f"{year + 1911:04d}-{month:02d}-{day:02d}"


# ── 訊號 1：法規頁上的修正日期 ──────────────────────────────────


def parse_page_modified_date(page_html: str) -> str | None:
    """從法規頁 HTML 抓出修正日期；從未修正過的法規則抓發布／公布日期。"""
    for label in ("修正日期", "發布日期", "公布日期"):
        match = re.search(_PAGE_DATE_RE.format(label=label), page_html)
        if match:
            return roc_to_iso(*(int(g) for g in match.groups()))
    return None


def fetch_page_modified_date(http: HttpClient, pcode: str) -> tuple[str, str]:
    """回傳（官網顯示的修正日期, 查詢的網址）。抓不到就明確報錯，不猜。"""
    url = page_url(pcode)
    page_html = http.get(url).decode("utf-8", errors="replace")
    modified = parse_page_modified_date(page_html)
    if not modified:
        raise SourceUnavailable(f"在法規頁上找不到修正日期（頁面格式可能已改變）：{url}")
    return modified, url


# ── 訊號 2：整包資料 ────────────────────────────────────────────


@dataclass
class TwArticle:
    no: str        # 標準條號，例如 '6-1'
    heading: str   # 所在的編／章／節
    content: str   # 條文原文


@dataclass
class TwLaw:
    pcode: str
    name: str
    eng_name: str
    modified_date: str            # 這一版的異動日期（YYYY-MM-DD）
    effective_note: str           # 「生效內容」註記（例如部分條文施行日期另定）
    histories: str                # 沿革全文
    bulk_update_date: str | None  # 整包資料本身的更新日期
    source_url: str
    articles: list[TwArticle] = field(default_factory=list)


def fetch_law(http: HttpClient, kind: str, pcode: str) -> TwLaw | None:
    """下載整包資料並取出指定法規；整包裡沒有這部法規時回傳 None。"""
    url = BULK_URLS[kind]
    raw = read_bulk_json(http.get(url, timeout=BULK_TIMEOUT_SECONDS))
    law = extract_law(raw, pcode)
    if law is None:
        return None
    return parse_law(law, pcode, parse_bulk_update_date(raw), url)


def read_bulk_json(zip_bytes: bytes) -> bytes:
    """從下載回來的 ZIP 裡讀出 JSON 檔（只在記憶體中處理，不解壓到硬碟）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            info = next(i for i in archive.infolist() if i.filename.lower().endswith(".json"))
            if info.file_size > MAX_BULK_JSON_BYTES:
                raise SourceUnavailable("整包資料異常地大，為保護記憶體不予處理")
            return archive.read(info)
    except (zipfile.BadZipFile, StopIteration) as exc:
        raise SourceUnavailable("下載到的整包資料不是預期的 ZIP 格式") from exc


def parse_bulk_update_date(raw: bytes) -> str | None:
    """整包資料開頭有一個 UpdateDate，例如 2026/11/13 上午 12:00:00。"""
    match = re.search(rb'"UpdateDate"\s*:\s*"(\d{4})/(\d{1,2})/(\d{1,2})', raw[:1000])
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def extract_law(raw: bytes, pcode: str) -> dict[str, Any] | None:
    """從整包 JSON 裡取出一部法規。

    整包檔案很大（命令那包解開超過 100MB），全部解析很吃記憶體，
    所以先用法規代碼定位，只解析那一部；萬一定位失敗，再退回完整解析。
    """
    wanted = f"pcode={pcode}"
    position = raw.find(f'{wanted}"'.encode())
    if position < 0:
        return None
    key = b'"LawLevel"'  # 每部法規的第一個欄位
    key_position = raw.rfind(key, 0, position)
    start = raw.rfind(b"{", 0, key_position) if key_position >= 0 else -1
    next_law = raw.find(key, position)
    if start >= 0:
        chunk = raw[start : next_law if next_law > 0 else len(raw)]
        try:
            law, _ = json.JSONDecoder().raw_decode(chunk.decode("utf-8"))
            if isinstance(law, dict) and str(law.get("LawURL", "")).endswith(wanted):
                return law
        except ValueError:
            pass
    # 退路：完整解析整包
    try:
        laws = json.loads(raw.decode("utf-8-sig")).get("Laws") or []
    except (ValueError, AttributeError) as exc:
        raise SourceUnavailable("整包資料不是預期的 JSON 格式") from exc
    return next((law for law in laws if str(law.get("LawURL", "")).endswith(wanted)), None)


def parse_law(
    law: dict[str, Any], pcode: str, bulk_update_date: str | None, source_url: str
) -> TwLaw:
    modified = str(law.get("LawModifiedDate") or "")
    if not re.fullmatch(r"\d{8}", modified):
        raise SourceUnavailable(f"整包資料中 {pcode} 缺少異動日期，無法判斷版本")
    result = TwLaw(
        pcode=pcode,
        name=str(law.get("LawName") or ""),
        eng_name=str(law.get("EngLawName") or ""),
        modified_date=f"{modified[:4]}-{modified[4:6]}-{modified[6:]}",
        effective_note=_join_wrapped(str(law.get("LawEffectiveNote") or "")),
        histories=str(law.get("LawHistories") or "").replace("\r\n", "\n").strip(),
        bulk_update_date=bulk_update_date,
        source_url=source_url,
    )
    # ArticleType：'C' 是編／章／節的標題，'A' 才是條文。
    # 用標題前面的縮排深淺，記住「目前在哪一編、哪一章、哪一節」。
    chapter_stack: list[tuple[int, str]] = []
    for item in law.get("LawArticles") or []:
        content = str(item.get("ArticleContent") or "")
        if item.get("ArticleType") == "C":
            indent = len(content) - len(content.lstrip(" "))
            while chapter_stack and chapter_stack[-1][0] >= indent:
                chapter_stack.pop()
            chapter_stack.append((indent, " ".join(content.split())))
            continue
        article_no = normalize_article_no(str(item.get("ArticleNo") or ""))
        if article_no is None:
            continue
        result.articles.append(
            TwArticle(
                no=article_no,
                heading=" > ".join(title for _, title in chapter_stack),
                content=content.replace("\r\n", "\n").strip(),
            )
        )
    return result


def last_history_entry(histories: str) -> str:
    """沿革的最後一筆，也就是最近一次修正的官方記載（原文照錄，只把斷行接回去）。"""
    entries: list[str] = []
    for line in histories.replace("\r\n", "\n").split("\n"):
        if re.match(r"^\d+\s*\.", line):
            entries.append(re.sub(r"^\d+\s*\.\s*", "", line).strip())
        elif entries:
            entries[-1] += line.strip()
    return re.sub(r"\s+", " ", entries[-1]).strip() if entries else ""


def _join_wrapped(text: str) -> str:
    """官方資料會在固定寬度硬斷行，這裡把斷行接回成一段。"""
    return re.sub(r"\s+", " ", "".join(line.strip() for line in text.splitlines())).strip()
