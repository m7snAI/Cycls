# pdf-extractor

Cycls remote function wrapping [pdf-inspector](https://github.com/firecrawl/pdf-inspector)
for Arabic PDF text extraction, so other agents can call it without vendoring
or building the Rust crate themselves.

## Usage

```python
from extract_pdf import extract_pdf

result = extract_pdf.func(pdf_bytes, mode="text")       # fast plain text
result = extract_pdf.func(pdf_bytes, mode="markdown")   # structured, slower

if result["error"]:
    ...  # bad/corrupt PDF, parser crash, etc. — content/page_count are empty/zero
else:
    print(result["content"])

# result = {"content": str, "mode": str, "page_count": int,
#           "warnings": list[str], "error": str | None}
```

A bad `pdf_bytes` value (corrupt, truncated, not actually a PDF) never
raises — it comes back as the same dict shape with `"error"` set and
`"content"`/`"page_count"`/`"warnings"` at their empty/zero defaults, so
callers check `result["error"]` instead of wrapping the call in
`try/except`. This also means one bad document in a
`cycls.remote("extract_pdf").map(pdf_list)` batch produces an error entry
for that item, not an aborted batch. A bad `mode` value is the one
exception — that's a caller bug, not a per-document problem, so it raises
`ValueError` immediately instead of returning an error dict.

`.func(...)` calls the underlying function directly (no Docker) — useful for
local testing. `.run(...)` / `.deploy(...)` go through the full Cycls
container path (not yet exercised — see Deployment status below).

## Source

Built from a local fork of pdf-inspector at `/tmp/pdf-inspector-fork`
(branch `fix/rtl-gap-formula`, commit `35626f4`), not the published PyPI
package — the PyPI wheel has none of the fixes below. The compiled
extension is vendored under `vendor/pdf_inspector/`.

## Fixes included (none on PyPI yet)

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

## ⚠️ Known limitation — not fixed

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
almost certainly why. Do not assume the fixes above cover it.

## `extract_text_bytes`/`extract_text` — do not use

These delegate straight to lopdf's generic `Document::extract_text()`,
bypassing pdf-inspector's entire custom pipeline (none of the three fixes
apply). Confirmed empirically: whole Arabic words come back individually
letter-reversed, one per line. `mode="text"` instead reconstructs plain
text in Python from `extract_text_with_positions_bytes()`, which does go
through the fixed pipeline. See `extract_pdf.py`'s module docstring for
the full detail — do not "simplify" this back to `extract_text_bytes`.

## Testing

```bash
python tests/test_extract_pdf.py
```

Verified against: the 3 original known-good fixtures, 24 real
`etimad_tender` tender-template PDFs, and 1 unrelated 357-page document,
in both modes. Full pdf-inspector regression suite (~1150 tests) passes
with 0 failures.

## Deployment status

**Not deployed.** Local testing only (`.func()` and direct wheel install).
Two things remain before an actual `.run()`/`.deploy()`:

- The vendored wheel is **macOS arm64** — Cycls containers build Linux
  images, so a Linux-platform wheel needs to be built from this same
  fixed source first.
- The Bug #2 / Instance 1 fixes are committed to the local fork but that
  fork has not been pushed or merged upstream.
