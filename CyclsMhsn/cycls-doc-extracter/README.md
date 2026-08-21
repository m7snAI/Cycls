# cycls-doc-extracter

Cycls remote function for multi-format document extraction: PDF via
[pdf-inspector](https://github.com/firecrawl/pdf-inspector) (patched fork,
for correct Arabic RTL text), and Office/OpenDocument/RTF/EPUB/CSV via
[anydoc](https://github.com/firecrawl/anydoc) (published PyPI package,
unmodified) — one function, one return shape, so other agents can call it
without vendoring or building either library themselves, and without
branching on file type to read the result.

## Usage

```python
import cycls

result = cycls.remote("cycls_doc_extracter")(pdf_bytes, "tender.pdf", mode="text")
result = cycls.remote("cycls_doc_extracter")(docx_bytes, "report.docx", mode="markdown")

if result["error"]:
    ...  # bad/corrupt file, unsupported extension, parser crash, etc. —
         # content/page_count are empty/zero
else:
    print(result["content"])

# result = {"content": str, "mode": str, "page_count": int,
#           "warnings": list[str], "error": str | None}
```

Or locally, without a deployment:

```python
from cycls_doc_extracter import cycls_doc_extracter

result = cycls_doc_extracter.func(file_bytes, "tender.pdf", mode="text")
```

A bad file (corrupt, truncated, wrong content for its extension) never
raises — it comes back as the same 5-key dict with `"error"` set and
`"content"`/`"page_count"`/`"warnings"` at their empty/zero defaults, so
callers check `result["error"]` instead of wrapping the call in
`try/except`. This also means one bad document in a
`cycls.remote("cycls_doc_extracter").map(files)` batch produces an error
entry for that item, not an aborted batch. A bad `mode` value is the one
exception — that's a caller bug, not a per-document problem, so it raises
`ValueError` immediately instead of returning an error dict.

`.func(...)` calls the underlying function directly (no Docker) — useful
for local testing. `.run(...)` / `.deploy(...)` go through the full Cycls
container path (not yet exercised — see Deployment status below).

## Supported formats

Dispatched by `filename`'s extension (case-insensitive):

| Extension(s) | Backend | Notes |
|---|---|---|
| `.pdf` | pdf-inspector (patched fork) | Arabic RTL fixes — see below |
| `.docx`, `.docm` | anydoc | |
| `.pptx`, `.pptm`, `.ppsx`, `.ppt` | anydoc | |
| `.xlsx`, `.xlsm`, `.xls` | anydoc | |
| `.odt`, `.odp`, `.ods` | anydoc | OpenDocument |
| `.rtf` | anydoc | |
| `.epub` | anydoc | |
| `.csv` | anydoc | |
| anything else | — | error dict: `"Unsupported file type: .{ext}"` |

Extension resolution (including the container aliases above) is anydoc's
own `format_from_extension`, not a hand-maintained list — if anydoc adds
formats, they're picked up automatically. `.doc` (legacy binary Word) is
also in anydoc's `Format` type and should work the same way, just not
exercised in this project's own sanity checks (see Coverage notes below).

## Mode behavior differs by backend

**PDF** (`pdf_backend.py`, unchanged from the original `extract_pdf.py`):
- `mode="text"` — fast plain text, reconstructed in Python from
  `pdf_inspector.extract_text_with_positions_bytes()` with the same
  RTL-aware line-grouping/gap logic as the Rust pipeline.
- `mode="markdown"` — slower, structured output with tables/headings/
  layout, via `pdf_inspector.process_pdf_bytes()`.
- `page_count` is the PDF's real page count.

**Everything else** (`anydoc_backend.py`): anydoc converts natively to
GitHub-Flavored Markdown; it has no native plain-text mode, so:
- `mode="markdown"` — native, exact: `anydoc.to_markdown_bytes()`.
- `mode="text"` — an approximation, not a faithful text-mode renderer:
  `anydoc.to_document()`'s structured block/inline model, flattened to
  plain text (paragraphs joined by blank lines, table cells space/pipe-
  joined per row, list items one per line). This drops Markdown syntax —
  no `#`, no `|` table formatting, no list markers/numbering — just the
  visible text in document order. **Use `mode="markdown"` when table
  grid or list structure matters; `mode="text"` is for when you just want
  the words.**
- `page_count` is **fixed at 1** for every anydoc format. anydoc's
  Document model has no page/slide/sheet count field, and none of these
  formats has one that's honestly derivable from it: docx/odt/rtf/doc are
  flow text with no layout engine (a real page count depends on fonts/
  margins/page size, which anydoc doesn't compute), and while pptx/xlsx
  do have a natural per-unit count (slides/sheets), it isn't safely
  inferable from the Document model either — e.g. anydoc's own sheet
  parser only emits a sheet-name heading when a workbook has *more than
  one* sheet, so a single-sheet `.xlsx` would silently read as 0 sheets
  if you counted headings. If you need a slide/sheet count, derive it
  yourself from a `mode="markdown"` result (e.g. count level-2 headings,
  with the same single-item caveat) rather than trust this field for
  non-PDF input.

## PDF fixes included (none on PyPI yet)

Built from a local fork of pdf-inspector at `/tmp/pdf-inspector-fork`
(branch `fix/rtl-gap-formula`, commit `35626f4`), not the published PyPI
package — the PyPI wheel has none of the fixes below. The compiled
extension is vendored under `vendor/pdf_inspector/`.

1. **Bug #1** (`014b4ff`) — word-level RTL merge used the LTR gap formula
   regardless of direction, so RTL words merged into individually-reversed,
   space-separated letters.
2. **Bug #2** (`35626f4`) — PDF producers pack multiple CIDs into one Tj/TJ
   operand under `BMC /ReversedChars`, storing them in visual order.
   pdf-inspector decoded the fused run in raw stream order, producing
   adjacent-letter-pair transpositions (e.g. "المعتمد" → "المعتدم"). Fixed
   by tracking the BMC/BDC tag and reversing the CID sequence when active.
3. **Instance 1** (`6d4cb5b`) — `expand_ligatures` reversed any decoded text
   containing Arabic presentation-form codepoints, wrongly assuming their
   presence meant visual-order storage. Some fonts use presentation forms
   purely as a glyph-shape encoding choice for already-correctly-ordered
   text. Fixed by dropping that automatic reversal (NFKC normalization is
   kept); genuine visual-order runs are still caught via `/ReversedChars`
   (Bug #2's fix).

### ⚠️ Known limitation — not fixed (PDF only)

Some producers store a short Arabic run in visual order with **no
structural signal at all** — no presentation forms, no `/ReversedChars`.
Confirmed on a real PowerPoint/Word-exported PDF (a 357-page networking
course document): the raw CIDs decode, byte-for-byte, straight to the wrong
order (e.g. "داخلي" → "يلخاد"), with nothing in the content stream to
detect or correct.

Fixing this needs real bidi heuristics (pen-position analysis, à la
MuPDF's `guess_bidi_level()`), which pdf-inspector does not implement.
**Neither of this wrapper's validation canaries catches it** — the words
are normal length and individually plausible, invisible to both the
isolated-single-letter check and the one-word-per-line check. If you see
scattered reversed words in output with no obvious pattern, this is
almost certainly why. Do not assume the fixes above cover it. This
limitation is specific to the PDF backend — anydoc's formats carry their
own logical reading order in the document structure, not positioned
glyphs, so this class of bug doesn't apply to them.

### `extract_text_bytes`/`extract_text` — do not use

These delegate straight to lopdf's generic `Document::extract_text()`,
bypassing pdf-inspector's entire custom pipeline (none of the three fixes
apply). Confirmed empirically: whole Arabic words come back individually
letter-reversed, one per line. `mode="text"` instead reconstructs plain
text in Python from `extract_text_with_positions_bytes()`, which does go
through the fixed pipeline. See `pdf_backend.py`'s module docstring for
the full detail — do not "simplify" this back to `extract_text_bytes`.

## anydoc: used unmodified, no fork

Unlike pdf-inspector, anydoc needed no patching. `.pip("firecrawl-anydoc")`
pulls the published PyPI package directly into the deployed image — no
vendoring, no local build. This was confirmed by reading the upstream
source (`/tmp/anydoc-investigation`, a clean checkout of
`github.com/firecrawl/anydoc` at the `v0.1.9` release tag, no local
modifications) and diffing its Python API against the newer `v0.2.3`
actually installed here — identical except two purely additive block/
inline kinds (`math`, `checkbox`). anydoc has its own fixture corpus,
snapshot tests, mutation tests (`tests/robustness.rs`), and `cargo-fuzz`
targets per format upstream — this project doesn't re-verify anydoc's own
conversion correctness, only that *this wrapper* calls it and flattens its
result correctly.

## Testing

```bash
python tests/test_cycls_doc_extracter.py
```

**PDF**: the same suite as before, unchanged — the 3 original known-good
fixtures, 24 real `etimad_tender` tender-template PDFs, and 1 unrelated
357-page document, in both modes (the last two aren't in this repo's own
test run, see the original recon). Full pdf-inspector regression suite
(~1150 tests) passes with 0 failures.

**docx/pptx/xlsx**: one codepoint-level sanity check per format, in both
modes, against anydoc's own "handmade" fixtures (`/tmp/anydoc-investigation/
tests/fixtures/`) — confirming known cell/text values survive this
wrapper's call and flatten unchanged. This checks the integration, not
anydoc's own correctness (see above). Also covered: a container-alias
extension (`.docm` → docx) and a case-insensitive extension
(`REPORT.DOCX`).

**Error paths**: an unsupported extension, a missing extension, a bad
`mode` (still raises `ValueError`), and invalid bytes against every one
of `.pdf`/`.docx`/`.pptx`/`.xlsx` (each must return a clean error dict,
never raise).

### Coverage notes — be honest about what this does and doesn't prove

- The docx/pptx/xlsx checks above are against **handmade + a couple of
  real-world fixtures from anydoc's own recon corpus**, not an exhaustive
  test of every layout anydoc can encounter (merged cells, nested lists,
  footnotes, embedded objects, RTL Office documents, etc. exist in
  anydoc's own fixture set but aren't separately re-asserted here).
  Confidence in *anydoc's* conversion breadth comes from its upstream
  test suite, not from this project.
- `.doc`, `.odt`, `.ods`, `.odp`, `.rtf`, `.epub`, `.csv` are supported via
  the same `resolve_format`/`extract_anydoc` path as docx/pptx/xlsx and
  are exercised by anydoc's own upstream tests, but have **no dedicated
  sanity check in this project's test suite** — extend `tests/
  test_cycls_doc_extracter.py` with one before relying on them in
  production.

## Deployment status

**Not deployed.** Local testing only (`.func()` and direct package
install/vendor). Two things remain before an actual `.run()`/`.deploy()`,
both specific to the PDF backend (anydoc has no equivalent issue — it's a
normal PyPI dependency the image installs at build time):

- The vendored `pdf_inspector` wheel is **macOS arm64** — Cycls containers
  build Linux images, so a Linux-platform wheel needs to be built from the
  same fixed source first.
- pdf-inspector's Bug #2 / Instance 1 fixes are committed to the local fork
  but that fork has not been pushed or merged upstream.
