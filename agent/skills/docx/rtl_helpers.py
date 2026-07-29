"""Reusable RTL/bilingual helpers for python-docx.

Use from a script in the workspace:

    import sys; sys.path.insert(0, ".skills/docx/scripts")
    from rtl_helpers import (setup_rtl, add_heading, add_paragraph, rtl_run,
                             ltr_run, rtl_paragraph, rtl_table, is_rtl)
    from docx import Document

    doc = Document(); setup_rtl(doc)                 # Arabic doc
    add_heading(doc, "عنوان المستند", level=1)
    add_paragraph(doc, "نص عربي يُعرض من اليمين إلى اليسار.")
    doc.save("out.docx")

For an English doc, just use plain python-docx (skip setup_rtl). For bilingual,
setup_rtl(doc) then mix rtl_run / ltr_run in the same paragraph, or use a
two-column table + rtl_table().
"""
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

AR_FONT = "IBM Plex Sans Arabic"   # default Arabic face (clean, full weight range)
AR_SANS = "Tajawal"                # alternate Arabic sans
EN_FONT = "Calibri"


def _apply(parent, tag, attrs=None):
    el = parent.find(qn(tag))
    if el is None:
        el = OxmlElement(tag)
        parent.append(el)
    for k, v in (attrs or {}).items():
        el.set(qn(k), v)
    return el


def is_rtl(text):
    """True if the text contains Arabic/Hebrew/etc. characters."""
    return any("؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ"
               or "ࢠ" <= c <= "ࣿ" or "ﭐ" <= c <= "﻿" for c in text)


def setup_rtl(doc, font=AR_FONT, bidi="ar-SA"):
    """Apply the document-wide RTL markers (markers 1-3). Call once per doc."""
    _apply(doc.settings.element, "w:themeFontLang", {"w:bidi": bidi})              # 1
    dd = _apply(doc.styles.element, "w:docDefaults")                              # 2
    rPr = _apply(_apply(dd, "w:rPrDefault"), "w:rPr")
    _apply(rPr, "w:rFonts", {"w:ascii": font, "w:hAnsi": font, "w:cs": font, "w:eastAsia": font})
    _apply(rPr, "w:rtl")
    _apply(_apply(_apply(dd, "w:pPrDefault"), "w:pPr"), "w:bidi")
    for section in doc.sections:                                                  # 3
        _apply(section._sectPr, "w:bidi")


def rtl_paragraph(p):
    """Mark a paragraph RTL (marker 4a). Don't set jc=right — Word mirrors it."""
    _apply(p._p.get_or_add_pPr(), "w:bidi")


def rtl_run(run, font=AR_FONT):
    """Mark an Arabic run RTL with the complex-script font (marker 4b)."""
    rPr = run._r.get_or_add_rPr()
    _apply(rPr, "w:rFonts", {"w:ascii": font, "w:hAnsi": font, "w:cs": font})
    _apply(rPr, "w:rtl")


def ltr_run(run, font=EN_FONT):
    """English run inside an RTL document — Latin font, no rtl marker."""
    rPr = run._r.get_or_add_rPr()
    _apply(rPr, "w:rFonts", {"w:ascii": font, "w:hAnsi": font})


def rtl_table(table):
    """Make table columns stack right-to-left."""
    _apply(table._tbl.tblPr, "w:bidiVisual")


# --- convenience builders (auto-direction by text) ---------------------------

def add_heading(doc, text, level=1, font=AR_FONT):
    h = doc.add_heading("", level=level)
    run = h.add_run(text)
    if is_rtl(text):
        rtl_paragraph(h); rtl_run(run, font)
    return h


def add_paragraph(doc, text, font=AR_FONT, bold=False):
    p = doc.add_paragraph()
    run = p.add_run(text); run.bold = bold
    if is_rtl(text):
        rtl_paragraph(p); rtl_run(run, font)
    else:
        ltr_run(run)
    return p


def bilingual_table(doc, rows, ar_header="العربية", en_header="English"):
    """rows: list of (arabic, english) tuples. Builds a 2-col RTL table."""
    t = doc.add_table(rows=1, cols=2); t.style = "Table Grid"
    hdr = t.rows[0].cells
    r = hdr[0].paragraphs[0].add_run(ar_header); rtl_paragraph(hdr[0].paragraphs[0]); rtl_run(r)
    r = hdr[1].paragraphs[0].add_run(en_header); ltr_run(r)
    for ar, en in rows:
        cells = t.add_row().cells
        ra = cells[0].paragraphs[0].add_run(ar); rtl_paragraph(cells[0].paragraphs[0]); rtl_run(ra)
        re = cells[1].paragraphs[0].add_run(en); ltr_run(re)
    rtl_table(t)
    return t


def validate(path):
    """Raise if RTL markers are missing in a doc that contains Arabic."""
    import zipfile
    z = zipfile.ZipFile(path)
    d = z.read("word/document.xml").decode()
    if is_rtl(d):
        st = z.read("word/styles.xml").decode()
        s = z.read("word/settings.xml").decode()
        assert "themeFontLang" in s and "<w:rtl/>" in st and "<w:bidi/>" in d, \
            "RTL markers missing — did you call setup_rtl(doc)?"
    return True
