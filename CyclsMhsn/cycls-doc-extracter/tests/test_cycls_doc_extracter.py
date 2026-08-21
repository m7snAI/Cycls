"""Local test: exercises cycls_doc_extracter's underlying logic directly
(via .func, bypassing the Cycls Docker build/run path).

PDF cases are the original extract_pdf test suite, unchanged, against the
three known Arabic tender fixtures. The anydoc cases are one codepoint-
level sanity check per new format (docx/pptx/xlsx), against anydoc's own
"handmade" fixtures — these check that *this wrapper* calls anydoc and
flattens its result correctly, not anydoc's own conversion correctness
(already established clean upstream — see README's Coverage notes).

Run: python tests/test_cycls_doc_extracter.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cycls_doc_extracter import cycls_doc_extracter

PDF_FIXTURES_DIR = Path("/tmp/pdf-inspector-fork/tests/fixtures")
PDF_FIXTURES = [
    "arabic_tender_sample.pdf",
    "arabic_tender_sample_2.pdf",
    "arabic_tender_sample_3.pdf",
]

ANYDOC_FIXTURES_DIR = Path("/tmp/anydoc-investigation/tests/fixtures")


def call(path: Path, filename: str, mode: str) -> dict:
    return cycls_doc_extracter.func(path.read_bytes(), filename, mode=mode)


def check_contract(result: dict, mode: str):
    assert set(result.keys()) == {"content", "mode", "page_count", "warnings", "error"}
    assert result["mode"] == mode


def test_pdf():
    for name in PDF_FIXTURES:
        path = PDF_FIXTURES_DIR / name
        for mode in ("text", "markdown"):
            result = call(path, name, mode)
            check_contract(result, mode)
            assert result["error"] is None
            assert isinstance(result["page_count"], int) and result["page_count"] > 0
            assert isinstance(result["content"], str) and len(result["content"]) > 0
            assert isinstance(result["warnings"], list)
            print(
                f"{name:35s} mode={mode:9s} pages={result['page_count']:3d} "
                f"content_len={len(result['content']):7d} warnings={result['warnings']}"
            )

    # Show one full result for visual inspection.
    sample = call(PDF_FIXTURES_DIR / "arabic_tender_sample.pdf", "arabic_tender_sample.pdf", "text")
    print("\n--- full result: arabic_tender_sample.pdf, mode=text ---")
    print("mode:", sample["mode"])
    print("page_count:", sample["page_count"])
    print("warnings:", sample["warnings"])
    print("content (first 500 chars):")
    print(sample["content"][:500])


def test_docx():
    path = ANYDOC_FIXTURES_DIR / "docx" / "handmade-rich.docx"

    markdown = call(path, "handmade-rich.docx", "markdown")
    check_contract(markdown, "markdown")
    assert markdown["error"] is None
    assert markdown["page_count"] == 1
    # Known content of anydoc's own "rich" fixture (also asserted in
    # anydoc's own test suite) — confirms this wrapper's call/plumbing,
    # not anydoc's conversion.
    assert "| Quarter | Widgets |" in markdown["content"]
    assert "| Q1 | 10 |" in markdown["content"]

    text = call(path, "handmade-rich.docx", "text")
    check_contract(text, "text")
    assert text["error"] is None
    # The flatten drops table pipes/markdown syntax but keeps the cell
    # values, space/pipe-joined.
    assert "Quarter | Widgets" in text["content"]
    assert "Q1 | 10" in text["content"]
    assert "Plan" in text["content"] and "Ship" in text["content"]
    print(f"docx  mode=markdown content_len={len(markdown['content'])}")
    print(f"docx  mode=text     content_len={len(text['content'])}")


def test_pptx():
    path = ANYDOC_FIXTURES_DIR / "pptx" / "pres.pptx"

    markdown = call(path, "pres.pptx", "markdown")
    check_contract(markdown, "markdown")
    assert markdown["error"] is None
    assert markdown["page_count"] == 1
    assert "Deck Title Slide" in markdown["content"]
    assert "| Region | Total |" in markdown["content"]

    text = call(path, "pres.pptx", "text")
    check_contract(text, "text")
    assert text["error"] is None
    assert "Deck Title Slide" in text["content"]
    assert "Top level point" in text["content"]
    assert "Region | Total" in text["content"]
    print(f"pptx  mode=markdown content_len={len(markdown['content'])}")
    print(f"pptx  mode=text     content_len={len(text['content'])}")


def test_xlsx():
    path = ANYDOC_FIXTURES_DIR / "xlsx" / "sheet.xlsx"

    markdown = call(path, "sheet.xlsx", "markdown")
    check_contract(markdown, "markdown")
    assert markdown["error"] is None
    assert markdown["page_count"] == 1
    assert "| Percent | 15.5% |" in markdown["content"]

    text = call(path, "sheet.xlsx", "text")
    check_contract(text, "text")
    assert text["error"] is None
    assert "Percent | 15.5%" in text["content"]
    print(f"xlsx  mode=markdown content_len={len(markdown['content'])}")
    print(f"xlsx  mode=text     content_len={len(text['content'])}")


def test_container_alias_and_case_insensitive_extension():
    # .docm is a container alias anydoc maps onto docx; extension matching
    # must also be case-insensitive.
    path = ANYDOC_FIXTURES_DIR / "docx" / "handmade-rich.docx"
    result = call(path, "REPORT.DOCX", "markdown")
    assert result["error"] is None
    assert "| Quarter | Widgets |" in result["content"]


def test_unsupported_extension_returns_error_dict():
    result = cycls_doc_extracter.func(b"whatever", "archive.zip", mode="text")
    assert result == {
        "content": "",
        "mode": "text",
        "page_count": 0,
        "warnings": [],
        "error": "Unsupported file type: .zip",
    }

    no_ext = cycls_doc_extracter.func(b"whatever", "README", mode="text")
    assert no_ext["error"] == "Unsupported file type: (no extension)"


def test_bad_mode_raises():
    try:
        cycls_doc_extracter.func(b"irrelevant", "x.pdf", mode="html")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "mode must be" in str(e)


def test_invalid_bytes_return_error_dict_not_raise():
    # A bad document must produce an error dict, not raise — this is what
    # lets cycls.remote(...).map() keep going past one bad item in a batch.
    for filename in ("not_a.pdf", "not_a.docx", "not_a.pptx", "not_a.xlsx"):
        result = cycls_doc_extracter.func(b"not a real document", filename, mode="text")
        assert result["error"], (filename, "expected a non-empty error message")
        assert result["content"] == ""
        assert result["page_count"] == 0
        assert result["warnings"] == []
        assert result["mode"] == "text"
        assert set(result.keys()) == {"content", "mode", "page_count", "warnings", "error"}
        print(f"{filename:12s} error: {result['error']}")


def main():
    test_pdf()
    print()
    test_docx()
    test_pptx()
    test_xlsx()
    test_container_alias_and_case_insensitive_extension()
    test_unsupported_extension_returns_error_dict()
    test_bad_mode_raises()
    print()
    test_invalid_bytes_return_error_dict_not_raise()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
