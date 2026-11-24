"""eCFR 轉接頭的測試：條號整理、XML 解析、版本挑選、搜尋結果整理。"""

import json

import pytest

from conftest import FakeHttp, VERSIONS_211_52, ecfr_titles, ecfr_version, ecfr_versions, fixture_bytes
from pharma_reg_mcp.http import SourceUnavailable
from pharma_reg_mcp.sources import ecfr


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("211.22", "211.22"),
        ("21 CFR 211.22", "211.22"),
        ("§ 211.22", "211.22"),
        ("21 CFR 211.22(a)", "211.22"),
        ("21 cfr 1308.11", "1308.11"),
        ("Part 211", None),
        ("quality control", None),
        ("", None),
    ],
)
def test_normalize_section(raw, expected):
    assert ecfr.normalize_section(raw) == expected


def test_urls_use_documented_endpoints():
    assert ecfr.section_url("211.52", "2025-04-01") == (
        "https://www.ecfr.gov/api/versioner/v1/full/2025-04-01/title-21.xml"
        "?part=211&section=211.52"
    )
    assert ecfr.web_url("211.52", "2025-04-01") == (
        "https://www.ecfr.gov/on/2025-04-01/title-21/section-211.52"
    )
    assert ecfr.citation("211.52") == "21 CFR 211.52"


def test_parse_section_xml_returns_heading_and_text():
    heading, text = ecfr.parse_section_xml(fixture_bytes("ecfr_211_52_current.xml"), "211.52")
    assert heading == "§ 211.52 Washing and toilet facilities."
    assert text.startswith("Adequate washing facilities shall be provided")
    assert "accesible" in text  # 現行版的原文就是這樣拼，程式不可以自作主張修正
    assert "<" not in text


def test_parse_section_xml_keeps_paragraphs_inline_marks_and_tables():
    raw = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<DIV8 N="1.1" TYPE="SECTION"><HEAD>\xc2\xa7 1.1 Demo.</HEAD>\n'
        b"<P>(a) First <I>paragraph</I>   with\n spaces.</P>\n<P>(b) Second.</P>\n"
        b'<DIV class="gpotbl_div"><TABLE><TR><TH>Item</TH><TH>Limit</TH></TR>'
        b"<TR><TD>A</TD><TD>10 mg</TD></TR></TABLE></DIV>\n"
        b"<CITA>[43 FR 45077, Sept. 29, 1978]</CITA></DIV8>"
    )
    heading, text = ecfr.parse_section_xml(raw, "1.1")
    assert heading == "§ 1.1 Demo."
    assert text.split("\n") == [
        "(a) First paragraph with spaces.",
        "(b) Second.",
        "Item | Limit",
        "A | 10 mg",
        "[43 FR 45077, Sept. 29, 1978]",
    ]


def test_parse_section_xml_rejects_a_different_section():
    """來源回了別條條文時要報錯，不可以將錯就錯。"""
    with pytest.raises(SourceUnavailable, match="不是所要求的條文"):
        ecfr.parse_section_xml(fixture_bytes("ecfr_211_52_current.xml"), "211.22")
    with pytest.raises(SourceUnavailable, match="無法解析"):
        ecfr.parse_section_xml(b"<html>blocked", "211.22")


def test_pick_version_chooses_the_one_in_force_on_the_date():
    assert ecfr.pick_version(VERSIONS_211_52, "2026-11-20")["date"] == "2025-05-27"
    assert ecfr.pick_version(VERSIONS_211_52, "2025-05-27")["date"] == "2025-05-27"
    assert ecfr.pick_version(VERSIONS_211_52, "2025-05-26")["date"] == "2016-11-29"
    assert ecfr.pick_version(VERSIONS_211_52, "2010-01-01") is None
    assert ecfr.pick_version([], "2026-11-20") is None


def test_fetch_title_status_reads_title_21(http: FakeHttp):
    http.add("titles.json", ecfr_titles(amended="2026-11-12", current="2026-11-20"))
    status = ecfr.fetch_title_status(http)
    assert status.latest_amended_on == "2026-11-12"
    assert status.up_to_date_as_of == "2026-11-20"
    assert status.import_in_progress is False


def test_fetch_title_status_fails_loudly_on_unexpected_response(http: FakeHttp):
    http.add("titles.json", b"<html>Request Access</html>")
    with pytest.raises(SourceUnavailable):
        ecfr.fetch_title_status(http)
    http.add("titles.json", json.dumps({"titles": []}).encode())
    with pytest.raises(SourceUnavailable, match="找不到 Title 21"):
        ecfr.fetch_title_status(http)


def test_fetch_versions_follows_pages(http: FakeHttp):
    http.add("versions/title-21.json", ecfr_versions([ecfr_version("211.1", "2025-12-18")], 2))
    http.add("page=2", ecfr_versions([ecfr_version("211.94", "2025-12-18")], 2))
    result = ecfr.fetch_versions(http, since="2025-01-01", part="211")
    assert [e["identifier"] for e in result.entries] == ["211.1", "211.94"]
    assert result.url == (
        "https://www.ecfr.gov/api/versioner/v1/versions/title-21.json"
        "?part=211&issue_date[gte]=2025-01-01"
    )
    assert result.truncated is False
    assert len(http.calls) == 2


def test_search_keeps_only_title_21_sections_and_strips_markup(http: FakeHttp):
    def hit(title, section, kind="Section"):
        return {
            "type": kind,
            "starts_on": "2016-11-29",
            "hierarchy": {"title": title, "part": "211", "section": section},
            "headings": {
                "part": "Current Good Manufacturing Practice",
                "section": "Responsibilities of <strong>quality</strong> control unit.",
            },
            "full_text_excerpt": 'a <strong>quality</strong> unit<span class="elipsis">…</span>end &amp; more',
        }

    payload = {
        "results": [hit("21", "211.22"), hit("32", "507.13"), hit("21", None, "Appendix")],
        "meta": {"total_count": 3},
    }
    http.add("search/v1/results", json.dumps(payload).encode())
    page = ecfr.search(http, "quality control unit", "2026-11-20", per_page=10)
    assert [r["section"] for r in page.results] == ["211.22"]
    assert page.results[0]["heading"] == "Responsibilities of quality control unit."
    assert page.results[0]["excerpt"] == "a quality unit…end & more"
    assert page.total == 3
    assert "agency_slugs[]=food-and-drug-administration" in page.url
    assert "date=2026-11-20" in page.url
