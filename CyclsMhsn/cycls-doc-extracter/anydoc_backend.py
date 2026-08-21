"""Office/OpenDocument/RTF/EPUB/CSV backend for cycls_doc_extracter, via
the published `firecrawl-anydoc` package (PyPI: firecrawl-anydoc, imports
as `anydoc`; github.com/firecrawl/anydoc). Unlike pdf-inspector, this is
used unmodified straight off PyPI — no fork, no vendoring: confirmed clean
(v0.1.9 source read, v0.2.3 installed — API identical except two purely
additive block/inline kinds) against its own fixture corpus plus this
project's own docx/pptx/xlsx sanity checks (see README).

anydoc converts natively to GitHub-Flavored Markdown; it has no native
plain-text mode. mode="markdown" calls anydoc.to_markdown_bytes() directly
— exact, native. mode="text" is an approximation: anydoc.to_document()
returns the structured block/inline model (headings, paragraphs, lists,
tables, block quotes), which _flatten_to_text() concatenates into plain
text, one block per paragraph break. This is a much simpler flatten than
the PDF backend's (no gap/RTL geometry to reconstruct — anydoc already
resolves reading order, including Arabic, from the document's own logical
structure, not positioned glyphs) but it drops Markdown syntax: no `#`,
no `|` table pipes, no list markers/numbering — just the visible text in
document order. Tables render as space-joined cells, one row per line, so
grid structure is lossy in "text" mode; use "markdown" when structure
matters.

page_count is fixed at 1 for every anydoc-handled format. anydoc's
Document model carries no page/slide/sheet count field, and none of these
formats has one to report honestly:
  - docx/odt/rtf/doc: flow text with no layout engine — a real page count
    depends on fonts/margins/page size, which anydoc doesn't compute.
  - pptx/ppt (slides) and xlsx/ods/xls (sheets) do have a natural per-unit
    count, but it isn't safely inferable from Document.blocks — e.g.
    anydoc's own sheet parser (src/formats/sheet/mod.rs) only emits a
    sheet-name heading when a workbook has *more than one* sheet, so a
    single-sheet xlsx produces zero headings even though the true count is
    one. Counting headings would silently misreport that case.
A caller that needs a slide/sheet count should derive it from a
mode="markdown" result itself (e.g. count level-2 headings, with the
same caveat above) rather than trust this field for non-PDF input.
"""


def resolve_format(ext: str):
    """The anydoc Format `ext` (without a leading dot) maps to — container
    aliases like docm/xlsm/ppsx/xls included, exactly as anydoc resolves
    them — or None if anydoc has nothing for it. Never returns "pdf":
    pdf-inspector (pdf_backend.py) handles that format in this project,
    not anydoc."""
    if not ext:
        return None
    import anydoc

    fmt = anydoc.format_from_extension(ext)
    return None if fmt == "pdf" else fmt


def extract_anydoc(file_bytes: bytes, fmt: str, mode: str) -> dict:
    """
    Convert an Office/OpenDocument/RTF/EPUB/CSV document via anydoc.

    fmt: an anydoc Format string (as returned by resolve_format), e.g.
         "docx", "pptx", "xlsx".
    mode: "markdown" — native GitHub-Flavored Markdown via
          anydoc.to_markdown_bytes(). "text" — plain-text flatten of
          anydoc.to_document()'s block model (see module docstring for
          exactly what that drops).

    `mode` is assumed already validated by the caller (cycls_doc_extracter)
    — this function only ever sees "text" or "markdown".

    Returns the same shape as pdf_backend.extract_pdf — see its docstring.
    Never raises: anydoc's typed errors (ConvertError and its subclasses —
    malformed/encrypted/unsupported/resource-limit/missing-part) and any
    other failure (bad bytes, import failure) are caught and returned as
    an error dict instead, so one bad document doesn't abort a
    `cycls.remote(...).map()` batch.
    """
    try:
        import anydoc

        if mode == "markdown":
            content = anydoc.to_markdown_bytes(file_bytes, fmt)
        else:
            document = anydoc.to_document(file_bytes, fmt)
            content = _flatten_to_text(document)

        return {
            "content": content,
            "mode": mode,
            "page_count": 1,
            "warnings": [],
            "error": None,
        }
    except Exception as e:
        return {
            "content": "",
            "mode": mode,
            "page_count": 0,
            "warnings": [],
            "error": str(e),
        }


def _flatten_to_text(document) -> str:
    """Plain-text flatten of anydoc's Document model: every Inline.text in
    document order, blocks joined with blank lines. See module docstring
    for what this drops relative to mode="markdown"."""

    def inlines_text(inlines):
        return "".join(i.text or "" for i in (inlines or []) if i.text)

    def block_text(block):
        if block.kind in ("heading", "paragraph"):
            return inlines_text(block.content)
        if block.kind == "code_block":
            return block.text or ""
        if block.kind == "list":
            lines = [
                block_text(b)
                for item in block.list.items
                for b in item.blocks
                if block_text(b)
            ]
            return "\n".join(lines)
        if block.kind == "table":
            lines = []
            for row in block.table.grid:
                cells = [
                    " ".join(t for b in slot.cell.blocks if (t := block_text(b)))
                    for slot in row
                    if slot.kind == "origin" and slot.cell
                ]
                if any(cells):
                    lines.append(" | ".join(cells))
            return "\n".join(lines)
        if block.kind == "block_quote":
            return "\n".join(t for b in (block.blocks or []) if (t := block_text(b)))
        return ""

    paragraphs = [t for b in document.blocks if (t := block_text(b))]
    return "\n\n".join(paragraphs)
