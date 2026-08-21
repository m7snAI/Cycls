"""Cycls remote function: PDF text extraction via pdf-inspector.

Wraps the pdf-inspector Rust crate (github.com/firecrawl/pdf-inspector,
built from the local fork at /tmp/pdf-inspector-fork, branch
fix/rtl-gap-formula, commit 35626f4) so other agents can call PDF
extraction without vendoring or building the Rust crate themselves.
Three fixes are included, none on PyPI yet:

  - Bug #1 (014b4ff): word-level RTL merge used the LTR gap formula
    regardless of direction, so RTL words merged into individually-
    reversed, space-separated letters.
  - Bug #2 (35626f4, src/extractor/content_stream.rs — MarkedContentEntry
    tag capture + reverse_cid_pairs()): PDF producers pack multiple CIDs
    into one Tj/TJ string operand under `BMC /ReversedChars`, storing them
    in visual (pre-reversed) order. pdf-inspector decoded that fused,
    multi-CID run in raw stream order — a real, narrow, adjacent-letter-
    pair transposition bug (e.g. "المعتمد" -> "المعتدم"), confirmed across
    all three known fixtures (all 3 use /ReversedChars on every page).
    Fixed by tracking the BMC/BDC tag name and reversing the CID sequence
    (not bytes within a CID) when that tag is active.
  - Instance 1 (6d4cb5b, src/text_utils.rs — expand_ligatures): the
    function reversed any decoded text containing Arabic presentation-form
    codepoints, on the wrong assumption that their presence meant
    visual-order storage. Some fonts' ToUnicode CMaps use presentation
    forms purely as a glyph-shape encoding choice for text that's already
    in correct logical order — confirmed on a real DINNextLTArabic PDF via
    dump_ops + ToUnicode CMap tracing, where "خطة"/"مشتريات" decoded
    correctly from the raw CIDs and were then wrongly reversed. Fixed by
    dropping the automatic reversal (NFKC normalization is kept); genuine
    visual-order runs remain handled via the actual structural signal for
    it, /ReversedChars (Bug #2's fix).

  ⚠️  KNOWN LIMITATION — NOT fixed, not addressed by any of the above:
  some producers store a short Arabic run in visual order with NO
  structural signal at all — no presentation forms, no /ReversedChars.
  Confirmed on a real PowerPoint/Word-exported PDF (a 357-page networking
  course document): the raw CIDs decode, byte-for-byte, straight to the
  wrong order (e.g. "داخلي" -> "يلخاد"), with nothing in the content stream
  to detect or correct. Fixing this needs real bidi heuristics (pen-
  position analysis, à la MuPDF's guess_bidi_level()), which this crate
  does not implement. If you see scattered reversed words in extracted
  output with no obvious pattern, this is almost certainly why — it is
  NOT something re-adding presentation-form or /ReversedChars handling
  would catch. Neither of this wrapper's validation canaries (below)
  detects it either: normal-length, individually-plausible words are
  invisible to both the isolated-single-letter and one-word-per-line
  checks.

The compiled extension is vendored under vendor/pdf_inspector/ (built via
`maturin build --release --features python` from that exact commit) and
copied into the container image rather than pip-installed from PyPI,
since the published 1.15.0 wheel has none of these fixes.

IMPORTANT — do not call pdf_inspector.extract_text_bytes()/extract_text():
those delegate straight to lopdf's generic Document::extract_text()
(src/extractor/mod.rs:62-75 in the crate), bypassing pdf-inspector's
entire custom CID/CMap/RTL pipeline (content_stream.rs, merge_text_items,
sort_line_items, expand_ligatures — everything all three fixes above live
in). Confirmed empirically: on these exact fixtures it returns whole
Arabic words individually letter-reversed, one per line (e.g. "واملواصفات"
-> "تافصاولماو" on its own line), unaffected by any of the fixes since it
never runs through that code. It is a real, separate, previously-
undocumented bug in the crate, not something this wrapper works around by
choice — do not "simplify" mode="text" back to extract_text_bytes without
re-reading this comment.

Instead, "text" mode calls extract_text_with_positions_bytes() (which DOES
go through the real, fixed pipeline) and reconstructs plain text in
Python using the same line-grouping + RTL-aware gap/sort logic as the
Rust merge_text_items() (src/extractor/mod.rs:979-1150+). This is a
faithful but simplified port: same y-tolerance line grouping, same RTL
detection, same directional sort, and the same corrected RTL gap formula
that Bug #1's fix introduced. It skips a few Rust-side refinements that
matter for markdown styling fidelity but not plain-text word/line order
(small-caps run detection, standalone-bullet spacing, Tw-inflated-width
capping) — see _reconstruct_plain_text() below for exactly what's ported.
Bug #2 needed no Python-side counterpart: it's fixed upstream of both
extract_text_with_positions_bytes() and process_pdf_bytes(), so both
"text" and "markdown" mode get it for free.
"""

import cycls

image = cycls.Image().copy("vendor/pdf_inspector", "vendor/pdf_inspector")


@cycls.function(image=image)
def extract_pdf(pdf_bytes: bytes, mode: str = "text") -> dict:
    """
    Extract text from a PDF (optimized/validated for Arabic RTL content)
    using pdf-inspector.

    mode: "text" (default) — fast plain text, reconstructed in Python from
          pdf_inspector.extract_text_with_positions_bytes() (see module
          docstring for why this isn't just extract_text_bytes).
          "markdown" — slower, structured output with tables/headings/
          layout, via pdf_inspector.process_pdf_bytes().

    Returns:
      {
        "content": str,          # extracted text or markdown
        "mode": str,             # echoes back which mode was used
        "page_count": int,
        "warnings": list[str],   # cheap integrity-canary findings, if any
        "error": str | None,     # set on failure; "content" etc. are then empty/zero
      }

    A bad `mode` raises ValueError immediately (a caller-programming error,
    not a per-document problem). Everything else — bad PDF bytes, a parser
    crash, an import failure — is caught and returned as an error dict
    instead of raised, so a `cycls.remote(...).map()` batch keeps going
    past one bad document instead of aborting the whole batch.
    """
    import os
    import sys

    if mode not in ("text", "markdown"):
        raise ValueError(f"mode must be 'text' or 'markdown', got {mode!r}")

    try:
        vendor_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
        if vendor_dir not in sys.path:
            sys.path.insert(0, vendor_dir)

        import pdf_inspector

        # --- RTL/CJK detection, ported from src/text_utils.rs (is_rtl_char /
        # is_cjk_char / is_rtl_text) so the line-direction decision below
        # matches the Rust pipeline exactly. ---

        def is_rtl_char(c: str) -> bool:
            cp = ord(c)
            return (
                0x0590 <= cp <= 0x05FF  # Hebrew
                or 0x0600 <= cp <= 0x06FF  # Arabic
                or 0x0700 <= cp <= 0x074F  # Syriac
                or 0x0750 <= cp <= 0x077F  # Arabic Supplement
                or 0x0780 <= cp <= 0x07BF  # Thaana
                or 0x07C0 <= cp <= 0x07FF  # NKo
                or 0x0800 <= cp <= 0x083F  # Samaritan
                or 0x0840 <= cp <= 0x085F  # Mandaic
                or 0x08A0 <= cp <= 0x08FF  # Arabic Extended-A
                or 0xFB1D <= cp <= 0xFB4F  # Hebrew Presentation Forms
                or 0xFB50 <= cp <= 0xFDFF  # Arabic Presentation Forms-A
                or 0xFE70 <= cp <= 0xFEFF  # Arabic Presentation Forms-B
            )

        def is_cjk_char(c: str) -> bool:
            cp = ord(c)
            return (
                0x1100 <= cp <= 0x11FF
                or 0x3000 <= cp <= 0x303F
                or 0x3040 <= cp <= 0x309F
                or 0x30A0 <= cp <= 0x30FF
                or 0x3130 <= cp <= 0x318F
                or 0x4E00 <= cp <= 0x9FFF
                or 0xAC00 <= cp <= 0xD7AF
                or 0xF900 <= cp <= 0xFAFF
                or 0xFF00 <= cp <= 0xFFEF
            )

        def is_rtl_text(texts) -> bool:
            rtl = ltr = 0
            for t in texts:
                for c in t:
                    if is_rtl_char(c):
                        rtl += 1
                    elif c.isalpha() and not is_cjk_char(c):
                        ltr += 1
            return rtl > 0 and rtl > ltr

        def is_arabic_letter(c: str) -> bool:
            cp = ord(c)
            return (
                0x0600 <= cp <= 0x06FF
                or 0x0750 <= cp <= 0x077F
                or 0xFB50 <= cp <= 0xFDFF
                or 0xFE70 <= cp <= 0xFEFF
            )

        def reconstruct_plain_text(items) -> str:
            """Port of merge_text_items' line-grouping + RTL-sort + gap-based
            spacing (src/extractor/mod.rs:979-1150+), operating on the raw
            pre-merge items from extract_text_with_positions_bytes(). See
            module docstring for what's intentionally simplified out."""
            if not items:
                return ""

            y_tolerance = 5.0
            groups = []  # list of [page, y, [items]]
            for it in items:
                placed = False
                for g in groups:
                    if g[0] == it.page and abs(it.y - g[1]) < y_tolerance:
                        g[2].append(it)
                        placed = True
                        break
                if not placed:
                    groups.append([it.page, it.y, [it]])

            ordered = []  # (page, y, sorted_items, rtl)
            for page, y, group in groups:
                rtl = is_rtl_text(i.text for i in group)
                group = sorted(group, key=lambda i: i.x, reverse=rtl)
                ordered.append((page, y, group, rtl))

            # Page ascending, then y descending (top of page first) — same as
            # the Rust `ordered_line_groups.sort_by(...)`.
            ordered.sort(key=lambda g: (g[0], -g[1]))

            lines_out = []
            for _, _, group, rtl in ordered:
                text = group[0].text
                end_x = group[0].x + group[0].width
                left_edge = group[0].x
                first_font_size = group[0].font_size

                for i in range(1, len(group)):
                    nxt = group[i]

                    # Break the run on a large font-size jump (distinct run,
                    # e.g. heading fragment vs. body text sharing a line).
                    if first_font_size > 0 and abs(nxt.font_size - first_font_size) > first_font_size * 0.3:
                        lines_out.append(text)
                        text = nxt.text
                        end_x = nxt.x + nxt.width
                        left_edge = nxt.x
                        first_font_size = nxt.font_size
                        continue

                    # Corrected RTL-aware gap formula — this is the exact
                    # bug Bug #1 fixed: the pre-fix code used the LTR formula
                    # unconditionally, producing large spurious negative gaps
                    # at every RTL junction, so adjacent RTL glyphs never
                    # merged into words.
                    if rtl:
                        gap = left_edge - (nxt.x + nxt.width)
                    else:
                        gap = nxt.x - end_x

                    font_size = first_font_size if first_font_size > 0 else 10.0
                    if gap > font_size * 0.5:
                        lines_out.append(text)
                        text = nxt.text
                        end_x = nxt.x + nxt.width
                        left_edge = nxt.x
                        continue
                    if gap < -font_size * 0.5:
                        lines_out.append(text)
                        text = nxt.text
                        end_x = nxt.x + nxt.width
                        left_edge = nxt.x
                        continue

                    prev_last = text.rstrip()[-1:] or None
                    next_first = nxt.text.lstrip()[:1] or None
                    if next_first is not None and next_first in ".,;)]}":
                        threshold = font_size * 0.25
                    elif (
                        prev_last is not None
                        and prev_last.islower()
                        and next_first is not None
                        and next_first.islower()
                    ):
                        threshold = font_size * 0.13
                    else:
                        threshold = font_size * 0.08

                    if gap > threshold:
                        text += " "
                    text += nxt.text

                    if rtl:
                        left_edge = min(left_edge, nxt.x)
                    else:
                        end_x = nxt.x + nxt.width

                lines_out.append(text)

            return "\n".join(lines_out)

        def validate(content: str) -> list[str]:
            found = []
            if not content:
                return found

            # Sign of a decode failure: an unusually high density of U+FFFD.
            fffd_count = content.count("�")
            if fffd_count > 0:
                density = fffd_count / len(content)
                if density > 0.005:  # >0.5% of all characters
                    found.append(
                        f"high U+FFFD replacement-character density "
                        f"({density:.2%}, {fffd_count} occurrences) — "
                        "possible decode failure"
                    )

            # Canary A — historical Bug #1 (word reversal with spurious
            # inter-letter spaces): a large fraction of whitespace-split
            # tokens that are a single isolated Arabic letter.
            tokens = content.split()
            if len(tokens) >= 20:
                isolated = sum(1 for t in tokens if len(t) == 1 and is_arabic_letter(t))
                fraction = isolated / len(tokens)
                if fraction > 0.15:
                    found.append(
                        f"{fraction:.1%} of tokens are isolated single Arabic "
                        f"letters ({isolated}/{len(tokens)}) — signature of the "
                        "historical word-reversal/spurious-space bug (Bug #1); "
                        "re-check output"
                    )

            # Canary B — the extract_text_bytes/lopdf-fallback failure mode
            # found while building this wrapper: whole multi-character words,
            # individually letter-reversed, one alone per line, instead of
            # normal paragraph flow. Distinct signature from Canary A (that
            # one is single *letters*; this one is single *words*, each
            # internally intact-length but out of order).
            lines = [l for l in content.split("\n") if l.strip()]
            if len(lines) >= 20:
                single_word_lines = sum(
                    1
                    for l in lines
                    if len(l.split()) == 1
                    and len(l.strip()) >= 2
                    and all(is_arabic_letter(c) for c in l.strip())
                )
                fraction = single_word_lines / len(lines)
                if fraction > 0.5:
                    found.append(
                        f"{fraction:.1%} of non-empty lines are a single "
                        f"Arabic word alone on its own line "
                        f"({single_word_lines}/{len(lines)}) — signature of a "
                        "naive/non-RTL-aware extraction path (e.g. lopdf's "
                        "generic extractor) that doesn't merge lines or "
                        "reorder text; re-check the extraction path"
                    )

            return found

        if mode == "text":
            items = pdf_inspector.extract_text_with_positions_bytes(pdf_bytes)
            content = reconstruct_plain_text(items)
            page_count = pdf_inspector.detect_pdf_bytes(pdf_bytes).page_count
        else:
            result = pdf_inspector.process_pdf_bytes(pdf_bytes)
            content = result.markdown or ""
            page_count = result.page_count

        return {
            "content": content,
            "mode": mode,
            "page_count": page_count,
            "warnings": validate(content),
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


# extract_pdf.local()
# extract_pdf.deploy()
