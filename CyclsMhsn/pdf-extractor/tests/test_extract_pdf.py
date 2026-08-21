"""Local test: exercises extract_pdf's underlying logic directly (via
.func, bypassing the Cycls Docker build/run path) against the three known
Arabic tender fixtures, in both modes.

Run: python tests/test_extract_pdf.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extract_pdf import extract_pdf

FIXTURES_DIR = Path("/tmp/pdf-inspector-fork/tests/fixtures")
FIXTURES = [
    "arabic_tender_sample.pdf",
    "arabic_tender_sample_2.pdf",
    "arabic_tender_sample_3.pdf",
]


def main():
    for name in FIXTURES:
        path = FIXTURES_DIR / name
        pdf_bytes = path.read_bytes()
        for mode in ("text", "markdown"):
            result = extract_pdf.func(pdf_bytes, mode=mode)
            assert result["mode"] == mode
            assert isinstance(result["page_count"], int) and result["page_count"] > 0
            assert isinstance(result["content"], str) and len(result["content"]) > 0
            assert isinstance(result["warnings"], list)
            assert result["error"] is None
            assert set(result.keys()) == {"content", "mode", "page_count", "warnings", "error"}
            print(
                f"{name:35s} mode={mode:9s} pages={result['page_count']:3d} "
                f"content_len={len(result['content']):7d} warnings={result['warnings']}"
            )

    # Show one full result for visual inspection.
    sample = extract_pdf.func(
        (FIXTURES_DIR / "arabic_tender_sample.pdf").read_bytes(), mode="text"
    )
    print("\n--- full result: arabic_tender_sample.pdf, mode=text ---")
    print("mode:", sample["mode"])
    print("page_count:", sample["page_count"])
    print("warnings:", sample["warnings"])
    print("content (first 500 chars):")
    print(sample["content"][:500])

    # A bad document must produce an error dict, not raise — this is what
    # lets cycls.remote(...).map() keep going past one bad item in a batch.
    bad_result = extract_pdf.func(b"not a pdf", mode="text")
    assert bad_result["error"], "expected a non-empty error message"
    assert bad_result["content"] == ""
    assert bad_result["page_count"] == 0
    assert bad_result["warnings"] == []
    assert bad_result["mode"] == "text"
    assert set(bad_result.keys()) == {"content", "mode", "page_count", "warnings", "error"}
    print("\n--- invalid PDF bytes ---")
    print("error:", bad_result["error"])


if __name__ == "__main__":
    main()
