"""台灣全國法規資料庫轉接頭的測試：條號整理、日期換算、整包資料解析。"""

import io
import json
import zipfile

import pytest

from conftest import ACT_HISTORIES, OTHER_LAW, FakeHttp, tw_bulk_zip, tw_law, tw_page
from pharma_reg_mcp.http import SourceUnavailable
from pharma_reg_mcp.sources import twlaw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("57", "57"),
        ("6-1", "6-1"),
        ("第 6-1 條", "6-1"),
        ("第6條之1", "6-1"),
        ("藥事法第 57 條", "57"),
        ("第 057 條", "57"),
        ("57條", "57"),
        ("第五十七條", None),  # v0.1 不支援國字數字，明確回報看不懂
        ("L0030001", None),
        ("", None),
    ],
)
def test_normalize_article_no(raw, expected):
    assert twlaw.normalize_article_no(raw) == expected


def test_roc_date_is_converted_to_western_calendar():
    assert twlaw.roc_to_iso(115, 3, 4) == "2026-03-04"
    assert twlaw.roc_to_iso(102, 7, 30) == "2013-07-30"


def test_parse_page_modified_date():
    assert twlaw.parse_page_modified_date(tw_page(115, 3, 4).decode()) == "2026-03-04"
    # 從未修正過的法規，頁面顯示的是發布日期
    assert twlaw.parse_page_modified_date(tw_page(102, 3, 11, "發布日期").decode()) == "2013-03-11"
    assert twlaw.parse_page_modified_date("<html>維護中</html>") is None


def test_fetch_page_modified_date_fails_loudly_when_page_layout_changes(http: FakeHttp):
    http.add("LawAll.aspx", "<html>系統維護中</html>".encode())
    with pytest.raises(SourceUnavailable, match="找不到修正日期"):
        twlaw.fetch_page_modified_date(http, "L0030001")


def test_fetch_law_extracts_the_right_law_from_bulk_zip(http: FakeHttp):
    http.add("api/Ch/Law/JSON", tw_bulk_zip([OTHER_LAW, tw_law(effective_note="部分條文施行\r\n日期另定。")]))
    law = twlaw.fetch_law(http, "Law", "L0030001")
    assert law.name == "藥事法"
    assert law.modified_date == "2026-03-04"
    assert law.bulk_update_date == "2026-11-13"
    assert law.effective_note == "部分條文施行日期另定。"
    assert [a.no for a in law.articles] == ["4", "5", "6-1", "61"]
    assert law.articles[0].content == "本法所稱藥物，係指藥品及醫療器材。"
    assert law.articles[0].heading == "第 一 章 總則"
    assert law.articles[3].heading == "第 五 章 藥物之販賣及製造"
    assert law.articles[3].content == "（刪除）"


def test_fetch_law_returns_none_when_law_is_not_in_bulk(http: FakeHttp):
    http.add("api/Ch/Law/JSON", tw_bulk_zip([OTHER_LAW]))
    assert twlaw.fetch_law(http, "Law", "L0030001") is None


def test_extract_law_falls_back_to_full_parse_for_unexpected_layout():
    """欄位順序和預期不同時（LawLevel 不在最前面），退回完整解析仍然找得到。"""
    law = tw_law()
    reordered = {"LawName": law["LawName"], **{k: v for k, v in law.items() if k != "LawName"}}
    raw = json.dumps({"UpdateDate": "2026/11/13", "Laws": [OTHER_LAW, reordered]}).encode()
    assert twlaw.extract_law(raw, "L0030001")["LawName"] == "藥事法"
    assert twlaw.extract_law(raw, "Z9999999") is None


def test_nested_chapter_headings_form_a_path():
    articles = [
        ("C", "", "第 二 編 藥品優良製造規範"),
        ("C", "", "   第 一 章 西藥"),
        ("A", "第 3 條", "西藥條文"),
        ("C", "", "   第 二 章 中藥"),
        ("C", "", "      第 一 節 通則"),
        ("A", "第 4 條", "中藥條文"),
        ("C", "", "第 三 編 附則"),
        ("A", "第 146 條", "本準則自發布日施行。"),
    ]
    law = twlaw.parse_law(tw_law(articles=articles), "L0030001", None, "u")
    assert [a.heading for a in law.articles] == [
        "第 二 編 藥品優良製造規範 > 第 一 章 西藥",
        "第 二 編 藥品優良製造規範 > 第 二 章 中藥 > 第 一 節 通則",
        "第 三 編 附則",
    ]


def test_parse_law_requires_a_modified_date():
    with pytest.raises(SourceUnavailable, match="缺少異動日期"):
        twlaw.parse_law(tw_law(modified=""), "L0030001", None, "u")


def test_last_history_entry_rejoins_wrapped_lines():
    assert twlaw.last_history_entry(ACT_HISTORIES) == (
        "中華民國一百十五年三月四日總統華總一義字第 11500017931 號令"
        "修正公布第 27-2、48-2、80、94、96-1、106 條條文；增訂第 27-3 條條文"
    )
    assert twlaw.last_history_entry("") == ""


def test_read_bulk_json_rejects_non_zip_and_zip_without_json():
    with pytest.raises(SourceUnavailable, match="不是預期的 ZIP"):
        twlaw.read_bulk_json(b"<html>error page</html>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "no json here")
    with pytest.raises(SourceUnavailable, match="不是預期的 ZIP"):
        twlaw.read_bulk_json(buffer.getvalue())


def test_urls():
    assert twlaw.page_url("L0030001") == "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=L0030001"
    assert twlaw.article_url("L0030001", "6-1") == (
        "https://law.moj.gov.tw/LawClass/LawSingle.aspx?pcode=L0030001&flno=6-1"
    )
    assert twlaw.citation("藥事法", "6-1") == "藥事法 第 6-1 條"
