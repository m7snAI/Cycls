"""Cycls remote function: multi-format document extraction.

Dispatches by filename extension to one of two backends:
  - .pdf -> pdf_backend.py, the pdf-inspector fork with the Arabic RTL
    fixes (see that module's docstring for the full bug history).
  - anything anydoc recognizes (.docx, .pptx, .xlsx, .odt, .rtf, .epub,
    .csv, and their container aliases like .docm/.xlsm/.ppsx/.xls) ->
    anydoc_backend.py, the published firecrawl-anydoc package, unmodified.
  - anything else -> an error dict, never a raise.

All three paths return the identical shape, so callers never need to
branch on file type to read a result:

  {"content": str, "mode": str, "page_count": int, "warnings": list[str],
   "error": str | None}

A bad `mode` is the one thing that still raises (ValueError) — it's a
caller-programming error, not a per-document problem, so it fails loudly
before any file is touched. Everything else that can go wrong (bad/corrupt
input, a parser crash, an import failure, an unrecognized extension) comes
back as an error dict with empty/zero defaults elsewhere, so a
`cycls.remote("cycls_doc_extracter").map(files)` batch keeps going past
one bad document instead of aborting the whole batch.
"""

import cycls

image = (
    cycls.Image()
    .copy("vendor/pdf_inspector", "vendor/pdf_inspector")
    .pip("firecrawl-anydoc")
)


@cycls.function(image=image)
def cycls_doc_extracter(file_bytes: bytes, filename: str, mode: str = "text") -> dict:
    """
    Extract text or markdown from a document, dispatched by `filename`'s
    extension.

    mode: "text" (default) — fast plain text. For PDF this is a faithful
          RTL-aware reconstruction (see pdf_backend.py); for anydoc
          formats it's a simpler flatten of the document's block model
          that drops Markdown structure (see anydoc_backend.py).
          "markdown" — structured output (tables/headings/layout) — via
          pdf-inspector for PDF, natively via anydoc for everything else.

    Returns:
      {
        "content": str,
        "mode": str,             # echoes back which mode was used
        "page_count": int,       # real PDF page count; a fixed 1 for
                                  # every anydoc format (see
                                  # anydoc_backend.py for why)
        "warnings": list[str],   # PDF integrity canaries; always [] for
                                  # anydoc formats
        "error": str | None,     # set on failure; other fields are then
                                  # empty/zero
      }
    """
    if mode not in ("text", "markdown"):
        raise ValueError(f"mode must be 'text' or 'markdown', got {mode!r}")

    try:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        if ext == "pdf":
            from pdf_backend import extract_pdf

            return extract_pdf(file_bytes, mode)

        from anydoc_backend import extract_anydoc, resolve_format

        fmt = resolve_format(ext)
        if fmt is None:
            return {
                "content": "",
                "mode": mode,
                "page_count": 0,
                "warnings": [],
                "error": f"Unsupported file type: .{ext}" if ext else "Unsupported file type: (no extension)",
            }
        return extract_anydoc(file_bytes, fmt, mode)
    except Exception as e:
        return {
            "content": "",
            "mode": mode,
            "page_count": 0,
            "warnings": [],
            "error": str(e),
        }


# cycls_doc_extracter.local()
# cycls_doc_extracter.deploy()
