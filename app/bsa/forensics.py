"""Fake / original checks for statement PDFs.

Evidence, strongest first:
  * balance chain breaks in an otherwise clean parse (numbers edited)
  * printed summary totals != sum of rows
  * digits in transaction rows drawn with an odd font / size (text pasted in)
  * two subsets of the same font on one page, incremental saves, overlay annotations, whiteout boxes
  * producer / creator is an editor (Word, Photoshop, Canva, online PDF editors)
  * PDF created before the last transaction date, or modified long after creation
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import fitz

from .banks import CERTIFIED
from .pdf_parser import frame_from_dict

# Any of these in producer / creator / author / XMP history => not a bank-generated original => FAKE.
# (Banks generate statements server-side with report engines; they never run a statement through these.)
THIRD_PARTY_TOOLS = [
    # online PDF tools (unlock / compress / merge / edit)
    "ilovepdf", "smallpdf", "sejda", "pdf2go", "pdfescape", "pdfcandy", "pdf candy", "sodapdf", "soda pdf", "pdf24",
    "online2pdf", "i2pdf", "pdfforge", "pdfcreator", "pdfsam", "docfly", "dochub", "pdffiller", "pdfgear", "pdf gear",
    "hipdf", "lightpdf", "xodo", "pdf-lib", "pdfsimpli", "pdfrun", "formswift", "pdf.io", "altoconvert", "freepdfconvert",
    # desktop editors / office
    "foxit", "nitro", "pdf-xchange", "pdfxchange", "wondershare", "pdfelement", "microsoft word", "microsoft® word",
    "word for", "microsoft excel", "microsoft® excel", "powerpoint", "libreoffice", "openoffice", "wps office",
    "kingsoft", "canva", "photoshop", "illustrator", "indesign", "gimp", "inkscape", "acrobat pro", "acrobat standard",
    "adobe acrobat", "abbyy", "finereader", "nuance", "power pdf", "kofax", "bluebeam", "pdf studio", "master pdf",
    "pdf architect", "able2extract", "icecream", "pdf expert", "pdfexpert", "pdfpen", "pdfill", "pdf candy",
    # online audit 2026-11-23: office exports that passed as ORIGINAL (never on a genuine sample)
    "pdfmaker", "microsoft publisher", "microsoft® publisher", "onlyoffice",
    # print / re-save / scan
    "print to pdf", "microsoft: print", "quartz pdfcontext", "mac os x", "macos", "ios version", "camscanner",
    "adobe scan", "genius scan", "pdfium", "skia/pdf", "chrome", "chromium", "firefox", "cutepdf", "dopdf",
    "bullzip", "novapdf", "primopdf",
    # scripting libraries used to strip passwords / rewrite files
    "qpdf", "pikepdf", "pypdf", "pdftk", "mutool", "pdfbox-app",
]
# Strong, unambiguous names we also look for anywhere in the raw file bytes (stripped Info dicts, XMP, fonts).
RAW_TOOL_MARKS = [b"ilovepdf", b"smallpdf", b"sejda", b"pdf2go", b"pdfescape", b"sodapdf", b"pdfcandy", b"camscanner",
                  b"pdf-xchange", b"wondershare", b"pdfelement", b"foxit", b"nitro pro", b"pdfgear", b"hipdf",
                  b"lightpdf", b"pdf24", b"online2pdf", b"i2pdf"]
# Writer marks (fraud sim 2026-11-23): PDF libraries copy the bank's Info dict on a re-save, so the producer still
# looks genuine - but every library serialises the file its own way. 0 of 111 samples carry these marks.
#  - MuPDF / PyMuPDF full save: binary comment '%\xc2\xb5\xc2\xb6' ('%µ¶') on the line after '%PDF-x.y'
#  - qpdf / pikepdf: binary comment '%\xbf\xf7\xa2\xfe'
#  - pypdf / PyPDF2 / PyPDF4: trailer '/ID [ <hex> <hex> ]' with spaces inside the brackets (among the samples only
#    FEDERAL_001 PyPDF4 and UJJIVAN_002 PyPDF2, both FAKE by producer already)
_REWRITER_HEADERS = {b"%\xc2\xb5\xc2\xb6": "MuPDF/PyMuPDF re-save", b"%\xbf\xf7\xa2\xfe": "qpdf/pikepdf re-save"}
_PYPDF_ID_RE = re.compile(rb"/ID\s*\[\s+<[0-9A-Fa-f]+>\s+<[0-9A-Fa-f]+>\s+\]")
# DIY PDF libraries (developer toolkits, not bank statement servers). Not FAKE on their own - genuine AU uses
# pdfmake, genuine RBL uses Qt - but on a certified bank where no genuine statement used them -> SUSPICIOUS.
DIY_LIBS = ["reportlab", "fpdf", "tcpdf", "react-pdf", "pdfmake", "jspdf", "dompdf", "wkhtmltopdf", "qt ",
            "pdfkit", "prawn", "weasyprint", "xhtml2pdf", "html-pdf", "puppeteer", "playwright", "mpdf"]
GENUINE_DIY = {"AU": ("pdfmake",), "RBL": ("qt ",)}   # seen on genuine certified samples (calibrate 2026-11-23)
GENUINE_BLANK_META = {"HDFC", "IDFC"}                 # genuine samples with no producer AND no creator
_XMP_TOOL_RE = re.compile(r"(?:pdf:Producer|xmp:CreatorTool)\s*(?:=\s*[\"'][^\"'\s][^\"']*[\"']|>\s*[^<\s][^<]*<)")


def _writer_marks(raw: bytes) -> tuple[list[str], list[str]]:
    """(re-writer names, DIY generator names) read from the file's own serialisation, whatever the metadata says."""
    head = raw[:1024]
    i = head.find(b"%PDF")
    nl = head.find(b"\n", i) if i >= 0 else -1
    line2 = head[nl + 1:nl + 6] if nl >= 0 else b""
    rew = [name for mark, name in _REWRITER_HEADERS.items() if line2.startswith(mark)]
    if _PYPDF_ID_RE.search(raw[-4096:]):
        rew.append("pypdf re-save")
    diy = ["reportlab"] if b"ReportLab Generated PDF document" in head else []
    return rew, diy


_OK_CH = set(" .-_/()®©™,:;+&@'")


_VAL_MONEY_RE = re.compile(r"^(?:INR|RS\.?|₹)?\s*-?[\d,]+\.\d{1,2}\s*(?:CR|DR)?$", re.I)
_VAL_DATE_RE = re.compile(r"\d{1,2}[-/ ](?:\d{1,2}|[A-Za-z]{3,9})[-/ ]\d{2,4}")
_VAL_NAME_RE = re.compile(r"^[A-Z][A-Z.' ]{3,}$")


def _filled_rects(dr: dict) -> list:
    """The rectangles a filled path really paints. dr['rect'] is the bounding box of the WHOLE path: one white path
    made of a header strip and a footer strip 'covers' the body in between (skeptic 2026-11-23, A_twopart_mask).
    Only plain boxes count - 're' items, one 'qu', or a closed axis-aligned 4-line box; any other shape is ignored."""
    items = dr.get("items") or []
    if items and all(it[0] == "re" for it in items):
        if len(items) > 1 and dr.get("even_odd"):
            return []  # outer box + hole = a frame, paints only the ring (skeptic 2026-11-23, F_frame)
        return [fitz.Rect(it[1]) for it in items]
    if len(items) == 1 and items[0][0] == "qu":
        q = items[0][1]
        return [q.rect] if q.is_rectangular else []
    if len(items) == 4 and all(it[0] == "l" for it in items):
        pts = [p for it in items for p in it[1:3]]
        if len({round(p.x, 1) for p in pts}) == 2 and len({round(p.y, 1) for p in pts}) == 2:
            return [fitz.Rect(dr["rect"])]
    return []


def _hidden_text_boxes(doc: fitz.Document, max_pages: int = 60) -> list[tuple[int, int]]:
    """[(page, spans hidden)] - opaque white fills painted AFTER visible text that fully cover it (a row, a
    narration or a total covered up). Page design paints backgrounds BEFORE the text: 0 of 111 samples have one,
    34 of 36 fraud-sim whole-row whiteouts do (2026-11-23). The old 'Whiteout boxes' check only saw small boxes."""
    out = []
    for pno in range(min(doc.page_count, max_pages)):
        page = doc[pno]
        spans = []
        for s in page.get_texttrace():
            txt = "".join(chr(c[0]) for c in s.get("chars", ())).strip()
            col = s.get("color") or (0, 0, 0)
            if txt and not all(c > 0.97 for c in col[:3]) and (s.get("opacity") or 1) > 0.5:
                spans.append((s.get("seqno", 0), fitz.Rect(s["bbox"])))
        if not spans:
            continue
        hidden = 0
        for dr in page.get_drawings():
            fill = dr.get("fill")
            # near-white too: a 0.96 grey box on a white page is invisible to the eye (skeptic 2026-11-23, E5)
            if not fill or not all(c > 0.94 for c in fill[:3]) or (dr.get("fill_opacity") or 1) < 0.9:
                continue
            for r in _filled_rects(dr):
                if r.width > 0.9 * page.rect.width and r.height > 0.9 * page.rect.height:
                    continue  # full-page background
                # >= 80 % of the text box covered (a box a pixel smaller than the text still hides it)
                hidden += sum(1 for sq, bb in spans if sq < dr.get("seqno", 0) and bb.width > 2 and bb.height > 1
                              and (r & bb).get_area() >= 0.8 * bb.get_area())
        if hidden:
            out.append((pno + 1, hidden))
    return out


def _inserted_font_values(doc: fitz.Document) -> list[tuple[int, str, str]]:
    """Values (money, dates / period, the holder name in the page-1 header) printed in a font that the whole file
    uses for nothing else - at most 3 text pieces, all of them values. A bank generator prints labels and values
    in its own few fonts; an editor that overwrites a name or a total embeds its own font for just those pieces
    (Indian Bank copy: holder name, period line and both totals in LiberationSans while the bank prints Arial).
    A rare font that also carries a label (INDIAN_001: 'Account Holder Name' + period line in ArialMT) is design."""
    spans, by_f = 0, defaultdict(list)
    for page in doc:
        h = page.rect.height
        for b in page.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    t = sp.get("text", "").strip()
                    if t:
                        spans += 1
                        by_f[re.sub(r"^[A-Z]{6}\+", "", sp.get("font", ""))].append(
                            (page.number, t, sp["bbox"][1] < 0.4 * h))
    out = []
    for f, ss in by_f.items():
        if len(ss) > 3 or len(ss) > 0.005 * spans:
            continue
        if all(_VAL_MONEY_RE.match(t.replace(" ", "")) or _VAL_DATE_RE.search(t)
               or (pno == 0 and top and _VAL_NAME_RE.match(t)) for pno, t, top in ss):
            out += [(pno + 1, f, t) for pno, t, _ in ss]
    return out


def _garbled(s: str) -> bool:
    """Metadata left encrypted after a tool stripped the password -> unreadable bytes like '^(J\xe4'."""
    if not s:
        return False
    ok = sum(1 for ch in s if (ch.isascii() and ch.isalnum()) or ch in _OK_CH)
    return ok / len(s) < 0.7 or not any(ch.isascii() and ch.isalnum() for ch in s)
EDITORS = THIRD_PARTY_TOOLS
REPROCESSORS: list[str] = []
GENERATORS = ["itext", "openpdf", "apache fop", "jasper", "crystal", "birt", "pdflib", "aspose", "wkhtmltopdf",
              "openhtmltopdf", "reporting services", "pdfmake", "tcpdf", "fpdf", "reportlab", "dompdf", "qt ",
              "finacle", "flexcube", "hgsbs", "sap netweaver", "oracle", "bi publisher", "e-statement", "aa.a",
              "pdfsharp", "pdfkit", "prawn", "jspdf", "documill", "streamserve", "exstream", "thunderhead",
              "ghostscript", "xsl formatter", "antenna house"]
SUSPECT_ANNOTS = {"FreeText", "Square", "Stamp", "Redact", "Polygon"}          # can hide / replace numbers
MARKUP_ANNOTS = {"Ink", "Highlight", "Underline", "StrikeOut", "Caret", "Line", "Circle", "Text", "Squiggly"}
AMOUNT_TXT_RE = re.compile(r"^[+-]?(\d{1,3}(,\d{2,3})+|\d+)\.\d{2}$")


def _pdf_date(s: str | None) -> datetime | None:
    if not s:
        return None
    m = re.search(r"(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", s)
    if not m:
        return None
    y, mo, d, h, mi, se = (int(g) if g else 0 for g in m.groups())
    try:
        return datetime(y, mo, d, h, mi, se)
    except ValueError:
        return None


def _pdf_utc(s: str | None) -> datetime | None:
    """PDF date -> naive UTC ('D:20261114130203+05'30'' -> 07:32:03); no offset printed = taken as UTC."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?\s*(Z|[+-])?(\d{2})?'?(\d{2})?", s or "")
    if not m:
        return None
    try:
        dt = datetime(*(int(g) if g else 0 for g in m.groups()[:6]))
    except ValueError:
        return None
    if m[7] in ("+", "-") and m[8]:
        off = timedelta(hours=int(m[8]), minutes=int(m[9] or 0))
        dt = dt - off if m[7] == "+" else dt + off
    return dt


def _classify_tool(s: str) -> str:
    low = (s or "").lower()
    if not low.strip():
        return "none"
    if any(k in low for k in THIRD_PARTY_TOOLS):
        return "third_party"
    if any(k in low for k in GENERATORS):
        return "generator"
    return "unknown"


def _tool_hits(text: str) -> list[str]:
    low = (text or "").lower()
    return sorted({k for k in THIRD_PARTY_TOOLS if k in low})


# ---------------------------------------------------------------- file history (incremental saves)
# Every save of a PDF ends with %%EOF. An incremental save appends its changes after the old bytes, so the bytes up
# to an earlier %%EOF are that earlier version of the file: the version the bank wrote can be opened and compared
# page by page with the final one. Seen 2026-11-20: a Canara iText statement re-saved 11 min later in Adobe Acrobat
# 'Edit text' - a person's name in 6 UPI credit narrations replaced by a company name (new font AcuminPro).
_EOF_RE = re.compile(rb"startxref\s+\d+\s*%%EOF[ \t]*(?:\r\n|\r|\n)?")
# fonts that a bank generator does not embed but an editor does -> likely tool (keys: upper case, no '-' / ' ')
_EDITOR_FONTS = {
    "ACUMINPRO": "Adobe Acrobat (Edit text)", "MINIONPRO": "Adobe Acrobat", "MYRIADPRO": "Adobe Acrobat",
    "ADOBEPISTD": "Adobe Acrobat", "CALIBRI": "Microsoft Word / Office", "CAMBRIA": "Microsoft Word / Office",
    "APTOS": "Microsoft Word / Office", "LIBERATIONSANS": "LibreOffice", "LIBERATIONSERIF": "LibreOffice",
    "CANVASANS": "Canva",
}


def _rev_open(data: bytes, password: str | None):
    d = fitz.open(stream=data, filetype="pdf")
    if d.needs_pass and not (password and d.authenticate(password)):
        return None
    return d


def _rev_words(page, skip=()) -> list[tuple[int, int, str]]:
    return [(round(w[0]), round(w[1]), w[4]) for w in page.get_text("words")
            if not any(r.contains(fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)) for r in skip)]


def _rev_fonts(doc) -> set[str]:
    return {re.sub(r"^[A-Z]{6}\+", "", f[3]) for p in doc for f in p.get_fonts() if f[3]}


def _rev_lines(items) -> list[tuple[int, int, str]]:
    """Words of one page -> line runs [(y, x, 'text')] (same y within 2pt)."""
    lines: dict = {}
    for x, y, w in sorted(items, key=lambda t: (t[1], t[0])):
        key = next((k for k in lines if abs(k - y) <= 2), y)
        lines.setdefault(key, []).append((x, w))
    return [(y, ws[0][0], " ".join(w for _, w in ws)) for y, ws in sorted(lines.items())]


def _rev_diff_page(pa, pb) -> tuple[list, list]:
    """Words that came / went between two versions of a page. Compared by the words themselves (an editor re-writes
    the whole page and shifts every word a fraction of a point); positions only place the words that changed."""
    sig = [w.rect for w in (pb.widgets() or []) if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE]
    la, lb = _rev_words(pa, sig), _rev_words(pb, sig)
    ta, tb = Counter(w for _, _, w in la), Counter(w for _, _, w in lb)
    add, rem = [], []
    for src, other, delta, out in ((lb, la, tb - ta, add), (la, lb, ta - tb, rem)):
        for w, n in delta.items():
            seen = [(x, y) for x, y, t in other if t == w]
            cand = [(x, y, t) for x, y, t in src if t == w
                    and not any(abs(x - ox) <= 3 and abs(y - oy) <= 3 for ox, oy in seen)]
            out += (cand or [(x, y, t) for x, y, t in src if t == w])[:n]
    return add, rem


def revision_history(raw: bytes, final: fitz.Document, password: str | None = None, max_pages: int = 400,
                     max_edits: int = 60) -> dict | None:
    """None for a file saved once. Else {revisions: [{n, date, bytes, producer, creator, signature, pages}],
    changes: [{from, to, minutes, comparable, signature, pages_changed, edits: [{page, before, after}], lines_changed,
    fonts_added, tool_hint, annots_added, pages_added, pages_removed}], text_changed, uncomparable, linearized}."""
    ends = [m.end() for m in _EOF_RE.finditer(raw)]
    if len(ends) < 2:
        return None
    linearized = b"/Linearized" in raw[:1024]
    if len(ends) > 25:  # every save re-opens the whole file: cap it (a bank never saves 25 times)
        return {"revisions": [], "changes": [], "text_changed": False, "uncomparable": True, "linearized": linearized}
    revs, docs = [], []
    for i, e in enumerate(ends):
        seg = raw[(ends[i - 1] if i else 0):e]
        info = {k.decode(): v.decode("latin-1") for k, v in
                re.findall(rb"/(Producer|Creator|CreationDate|ModDate)\s*\(([^)]{0,80})\)", seg)}
        d = final if i == len(ends) - 1 else None
        if d is None:
            try:
                d = _rev_open(raw[:e], password)
            except Exception:
                d = None
        # a linearized file ends its first-page section with an %%EOF too: not a save of its own
        if linearized and i == 0 and e < len(raw) // 2 and (d is None or d.page_count < final.page_count):
            continue
        # date / producer from this version's own Info, read through MuPDF so encrypted strings are decrypted and a
        # ')' inside the producer does not cut it. A later save that wrote no Info entry of its own is undated and
        # has no producer of its own (it does not inherit the first save's date)
        md = (d.metadata or {}) if d is not None else {}
        def own(key: bytes) -> bool:
            return not revs or bool(re.search(rb"/" + key + rb"(?![A-Za-z])", seg))
        if d is not None:
            # the save's own plain-text date first (Acrobat writes it into the appended section while MuPDF can still
            # report the older Info); MuPDF's decrypted value only when the raw string is unreadable (encrypted)
            raw_date = info.get("ModDate") or info.get("CreationDate")
            date_s = raw_date if _pdf_date(raw_date) else (
                (md.get("modDate") or md.get("creationDate")) if own(b"ModDate") or own(b"CreationDate") else None)
            producer = (md.get("producer") or None) if own(b"Producer") else None
            creator = (md.get("creator") or None) if own(b"Creator") else None
        else:
            date_s = info.get("ModDate") or info.get("CreationDate")
            producer, creator = info.get("Producer"), info.get("Creator")
        revs.append({"n": len(revs) + 1, "date": _pdf_date(date_s), "utc": _pdf_utc(date_s), "bytes": len(seg),
                     "producer": producer, "creator": creator, "signature": b"/ByteRange" in seg,
                     "pages": d.page_count if d is not None else None})
        docs.append(d)
    if len(revs) < 2:
        return None
    changes = []
    for i in range(1, len(revs)):
        a, b = docs[i - 1], docs[i]
        ch = {"from": revs[i - 1]["n"], "to": revs[i]["n"], "minutes": None, "comparable": a is not None and b is not None,
              "signature": revs[i]["signature"], "pages_changed": [], "edits": [], "lines_changed": 0,
              "fonts_added": [], "tool_hint": None, "annots_added": 0, "pages_added": 0, "pages_removed": 0,
              "graphics_changed": []}
        if revs[i - 1]["utc"] and revs[i]["utc"]:  # both in UTC: an IST date vs a 'Z' date is not 330 min apart
            ch["minutes"] = round((revs[i]["utc"] - revs[i - 1]["utc"]).total_seconds() / 60, 1)
        if ch["comparable"]:
            ch["pages_added"] = max(0, b.page_count - a.page_count)
            ch["pages_removed"] = max(0, a.page_count - b.page_count)
            ch["fonts_added"] = sorted(_rev_fonts(b) - _rev_fonts(a))
            for pno in range(min(a.page_count, b.page_count, max_pages)):
                pa, pb = a[pno], b[pno]
                ch["annots_added"] += max(0, len(list(pb.annots() or [])) - len(list(pa.annots() or [])))
                add, rem = _rev_diff_page(pa, pb)
                # drawing changed without a text change (white box, image, moved word): no 'pass' for this save
                nonsig = lambda p: sum(1 for w in (p.widgets() or []) if w.field_type != fitz.PDF_WIDGET_TYPE_SIGNATURE)
                if pa.read_contents() != pb.read_contents() or nonsig(pb) > nonsig(pa):
                    ch["graphics_changed"].append(pno + 1)
                if not (add or rem):
                    continue
                ch["pages_changed"].append(pno + 1)
                # one edit = changed lines within 14pt of each other (an editor re-wraps the cell it touched:
                # 2 old lines can come back as 3), old text joined vs new text joined
                lines = sorted([(y, "-", t) for y, _x, t in _rev_lines(rem)] + [(y, "+", t) for y, _x, t in _rev_lines(add)])
                block: list = []
                for ln in lines + [(10 ** 9, "", "")]:
                    if block and ln[0] - block[-1][0] > 14:
                        ch["edits"].append({"page": pno + 1, "y": block[0][0],
                                            "before": " ".join(t for _, s, t in block if s == "-")[:200],
                                            "after": " ".join(t for _, s, t in block if s == "+")[:200]})
                        block = []
                    block.append(ln)
            ch["lines_changed"] = len(ch["edits"])
            ch["edits"] = sorted(ch["edits"], key=lambda e: (e["page"], e["y"]))[:max_edits]
            hints = sorted({tool for f in ch["fonts_added"] for key, tool in _EDITOR_FONTS.items()
                            if key in re.sub(r"[-\s]", "", f.upper())})
            ch["tool_hint"] = ", ".join(hints) or None
        changes.append(ch)
    for d in docs[:-1]:  # earlier versions are separate in-memory copies of the file: free them now
        if d is not None and d is not final:
            d.close()
    return {"revisions": [{k: (v.isoformat() if k == "date" and v else v) for k, v in r.items() if k != "utc"}
                          for r in revs],
            "changes": changes, "text_changed": any(c["lines_changed"] or c["pages_added"] or c["pages_removed"]
                                                    for c in changes),
            "uncomparable": any(not c["comparable"] for c in changes), "linearized": linearized}


def analyze_pdf(doc: fitz.Document, raw_bytes: bytes, txns, recon: dict, last_txn_date: date | None,
                was_encrypted: bool, parse_conf: float, header_text: str = "", password: str | None = None,
                bank_code: str | None = None, recognised: bool = True) -> dict:
    """recognised=False: verify-only file where neither a bank nor any transaction could be read."""
    checks: list[dict] = []
    penalty = 0
    hard_fail = False
    markup = Counter()

    def add(name, status, detail, pen=0):
        nonlocal penalty
        checks.append({"name": name, "status": status, "detail": detail})
        penalty += pen

    meta = doc.metadata or {}
    producer, creator = (meta.get("producer") or "").strip(), (meta.get("creator") or "").strip()
    author = (meta.get("author") or "").strip()
    created, modified = _pdf_date(meta.get("creationDate")), _pdf_date(meta.get("modDate"))
    eof = raw_bytes.count(b"%%EOF")
    signed = b"/ByteRange" in raw_bytes and b"/Sig" in raw_bytes
    fake_reasons: list[str] = []
    try:
        xmp = doc.get_xml_metadata() or ""
    except Exception:
        xmp = ""

    # --- text layer: bank e-statements always have one
    pages = doc.page_count
    textless = sum(1 for p in doc if len(p.get_text("text").strip()) < 30)
    scanned = pages and textless / pages > 0.5
    if scanned:
        add("Text layer", "fail", f"{textless}/{pages} pages are images (scanned / photographed / screenshot). "
                                  "Bank e-statements are always text PDFs - this is not a bank-generated file.", 60)
        fake_reasons.append("scanned / image-only PDF")
    else:
        add("Text layer", "pass", "Machine-generated text layer present.")
        if not recognised:
            # online audit 2026-11-23: react-pdf / Word / Publisher documents with no bank and no rows -> ORIGINAL 90.
            # Only a file that doesn't even read like a statement is marked down: a genuine small bank missing from our
            # dictionary with a layout we can't parse yet is OUR gap, not the customer's (skeptic 2026-11-23, V1)
            head = " ".join(doc[i].get_text("text") for i in range(min(pages, 2))).lower()
            stmt_like = "balance" in head and bool(re.search(r"account|a/c|statement", head))
            if stmt_like:
                add("Bank statement", "info", "Bank not recognised and no transactions could be read - only the file "
                    "checks ran. Confirm the bank and the figures by eye.")
            else:
                add("Bank statement", "warn", "No bank (name / IFSC), no transactions and no balance / account "
                    "wording - this file does not look like a bank statement. Only statements downloaded from the "
                    "bank count.", 25)

    # --- producer / creator / author / XMP history / raw bytes
    tools = {producer, creator, author} - {""}
    tool_txt = " / ".join(sorted(tools)) or "none"
    hits = set()
    for t in tools:
        hits.update(_tool_hits(t))
    xmp_hits = set(_tool_hits(xmp)) - hits
    low_bytes = raw_bytes.lower()
    raw_hits = {m.decode() for m in RAW_TOOL_MARKS if m in low_bytes} - hits - xmp_hits
    rewriters, diy_marks = _writer_marks(raw_bytes)
    raw_hits |= set(rewriters)   # a library re-save that kept the bank's metadata (fraud sim 2026-11-23)
    kinds = {t: _classify_tool(t) for t in tools}
    if hits or xmp_hits or raw_hits:
        where = []
        if hits:
            where.append(f"metadata says {tool_txt}")
        if xmp_hits:
            where.append(f"XMP edit history mentions {', '.join(sorted(xmp_hits))}")
        if raw_hits:
            where.append(f"file contains traces of {', '.join(sorted(raw_hits))}")
        add("Producer / creator / author", "fail", "Passed through a third-party PDF tool - " + "; ".join(where) +
            ". Banks never produce statements with these tools, so this is not the original bank file "
            "(unlocked, re-saved or edited). Ask for the original password-protected PDF from the bank.", 60)
        fake_reasons.append("third-party PDF tool: " + ", ".join(sorted(hits | xmp_hits | raw_hits)))
    elif any(_garbled(t) for t in tools) and b"/Encrypt" in raw_bytes:
        # still encrypted: bank generator wrote plain-text Info strings, the reader "decrypted" them into noise
        m = re.search(rb"/Producer\s*\(([ -~]{0,60})\)", raw_bytes)
        raw_prod = m.group(1).decode() if m else "?"
        producer = f"{raw_prod} (raw)"
        add("Producer / creator", "info", f"Bank-encrypted file; producer stored as plain text '{raw_prod}'.")
    elif any(_garbled(t) for t in tools):
        add("Producer / creator / author", "fail", f"Metadata is unreadable bytes ({tool_txt!r}). This happens when a "
            "tool removes the bank's password but leaves the metadata encrypted - the file is not the original "
            "bank download. Ask for the original password-protected PDF.", 60)
        fake_reasons.append("password stripped by a tool (garbled metadata)")
    elif any(k == "generator" for k in kinds.values()):
        add("Producer / creator", "pass", f"Report-engine output ({tool_txt}).")
    elif not tools and not _XMP_TOOL_RE.search(xmp) and bank_code in CERTIFIED and bank_code not in GENUINE_BLANK_META:
        # (a generator that writes its name only in XMP - PDF 2.0 style - is not 'blank': skeptic 2026-11-23, T1)
        # fixtures 2026-11-23: script-made 'AXIS/ICICI/SBI' statements with no metadata passed as ORIGINAL 95 READY
        add("Producer / creator", "warn", f"No producer/creator metadata - never the case on the genuine {bank_code} "
            f"statements we have seen ({CERTIFIED[bank_code]} certified). Ask for a fresh download from the bank.", 25)
    elif not tools:
        add("Producer / creator", "warn", "No producer/creator metadata (stripped).", 5)
    else:
        add("Producer / creator", "info", f"Unrecognised tool: {tool_txt}.")
    if not (hits or xmp_hits or raw_hits):
        low_tools = " / ".join(sorted(tools)).lower()
        diy = sorted({k.strip() for k in DIY_LIBS if k in low_tools})
        if (diy_marks and not any(m in low_tools for m in diy_marks) and any(k == "generator" for k in kinds.values())
                and eof == 1 and not signed):
            # written by ReportLab in one go, yet the metadata names another report engine (iText, Jasper...):
            # the metadata was rewritten. A bank-branded producer string ('X Bank eStatement Service', set with
            # canvas.setProducer) or a later signing / stamping save is legitimate - skeptic 2026-11-23, G1/G2 -
            # so no fake reason: one fail = SUSPICIOUS on its own
            add("Generator mismatch", "fail", f"File written by {', '.join(diy_marks)} but its metadata names another "
                f"report engine ('{tool_txt}') - the metadata looks rewritten.", 30)
        elif (diy or diy_marks) and bank_code in CERTIFIED and \
                not all(any(g.strip() == d for g in GENUINE_DIY.get(bank_code, ())) for d in (diy or diy_marks)):
            # fixtures + fraud sim 2026-11-23: ReportLab / react-pdf 'HDFC / SBI' rebuilds passed as ORIGINAL 100
            add("Generator vs bank", "warn", f"Made with {', '.join(diy or diy_marks)} (a developer PDF library) - never "
                f"seen on a genuine {bank_code} statement. Bank statement servers use report engines; ask for a "
                "fresh download from net banking.", 25)

    # --- dates
    if created and modified and (modified - created).total_seconds() > 300:
        gap = modified - created
        add("Created vs modified", "warn", f"Modified {gap.days}d {gap.seconds // 3600}h after creation "
                                           f"({created:%d-%b-%Y %H:%M} -> {modified:%d-%b-%Y %H:%M}).", 15)
    elif created:
        add("Created vs modified", "pass", f"Created {created:%d-%b-%Y %H:%M}; not modified later.")
    if created and last_txn_date and created.date() < last_txn_date and (last_txn_date - created.date()).days > 1:
        add("Creation date vs transactions", "fail",
            f"PDF claims creation on {created:%d-%b-%Y} but has transactions up to {last_txn_date:%d-%b-%Y}.", 35)
        fake_reasons.append("created before its own last transaction")

    # --- incremental saves / signature
    if signed:
        add("Digital signature", "pass", "Document carries a digital signature block (verify signer in a PDF reader).")
        penalty -= 5
    try:
        history = revision_history(raw_bytes, doc, password)
    except Exception:
        history = None
    edited = [c for c in (history or {}).get("changes", [])
              if c["comparable"] and (c["lines_changed"] or c["pages_added"] or c["pages_removed"])]
    def _hard(c):
        # hard proof: an editor's font / tool, pages added or dropped, another producer, or a later sitting.
        # A generator's own second pass a few minutes later in its own fonts stays SUSPICIOUS (one fail)
        r0, r1 = history["revisions"][c["from"] - 1], history["revisions"][c["to"] - 1]
        p0, p1 = (r0["producer"] or "").strip(), (r1["producer"] or "").strip()
        # iText's append mode keeps the producer and adds '; modified using iText ..': not another producer
        return bool(c["fonts_added"] or c["tool_hint"] or c["pages_added"] or c["pages_removed"]
                    or (p0 and p1 and not p1.startswith(p0))
                    or c["minutes"] is None or c["minutes"] > 10)

    graphics = sorted({p for c in (history or {}).get("changes", []) for p in c.get("graphics_changed", [])})
    if edited:
        hard = [x for x in edited if _hard(x)]
        c = (hard or edited)[0]  # describe the save that proves the edit, not an earlier soft one
        when = f"{c['minutes']:g} min after the previous save" if c["minutes"] is not None else "in a later, undated save"
        parts = [f"text changed on {len(c['pages_changed'])} page(s) {c['pages_changed'][:8]}"] if c["pages_changed"] else []
        parts += [f"{c['pages_added']} page(s) added"] if c["pages_added"] else []
        parts += [f"{c['pages_removed']} page(s) removed"] if c["pages_removed"] else []
        parts += ["new font " + ", ".join(c["fonts_added"][:3])] if c["fonts_added"] else []
        add("Edited after generation", "fail",
            f"Saved again {when}{' with ' + c['tool_hint'] if c['tool_hint'] else ''}: " + "; ".join(parts)
            + f" ({sum(x['lines_changed'] for x in edited)} edit(s) vs the first version) - see File "
              "history.", 35)
        if hard:
            fake_reasons.append("page text changed after the file was first saved")
    elif graphics and history and not history["uncomparable"]:
        add("Incremental saves", "warn", f"A later save changed the page drawing on page(s) {graphics[:8]} but no text "
            "(white box, image or moved text?) - compare those pages with a fresh download.", 15)
    elif history and not history["uncomparable"]:
        what = ["a digital signature"] if any(c["signature"] for c in history["changes"]) else []
        ann = sum(c["annots_added"] for c in history["changes"])
        what += [f"{ann} annotation(s)"] if ann else []
        n = len(history["changes"])
        add("Incremental saves", "pass", f"{n} later save(s) changed no page text"
            + (f" (added {', '.join(what)})." if what else " (metadata / structure only)."))
    elif eof > 2 or (eof == 2 and not signed and not was_encrypted):
        add("Incremental saves", "warn", f"{eof} save sections (%%EOF) - the file was appended to after it was "
                                         "first written.", 15 if eof > 2 else 8)
    else:
        add("Incremental saves", "pass", f"{eof} save section(s).")

    if was_encrypted:
        add("Password protection", "pass", "File was password-protected like bank e-statements usually are.")

    # --- annotations / whiteout boxes / font subsets / odd fonts
    annot_hits = Counter()
    subset_pages = []
    subset_sigs: set = set()  # which fonts are embedded twice, and how often, page by page
    subset_font_pages = 0
    whiteouts = 0
    rows_by_page = defaultdict(list)
    for t in txns:
        rows_by_page[t.page].extend((tp, t) for tp in (t.tops or []))
    font_counter: Counter = Counter()
    font_rows: dict = defaultdict(list)
    scan_pages = sorted(rows_by_page)[:400]
    for pno in range(pages):
        page = doc[pno]
        for a in page.annots() or []:
            if a.type[1] in SUSPECT_ANNOTS:
                annot_hits[a.type[1]] += 1
            elif a.type[1] in MARKUP_ANNOTS:
                markup[a.type[1]] += 1
        bases = defaultdict(set)
        for f in page.get_fonts():
            name = f[3] or ""
            if "+" in name:
                pre, base = name.split("+", 1)
                bases[base].add(pre)
        dup_sig = frozenset((b, len(v)) for b, v in bases.items() if len(v) > 1)
        if dup_sig:
            subset_pages.append(pno + 1)
            subset_sigs.add(dup_sig)
        if bases:
            subset_font_pages += 1
    for pno in scan_pages:
        page = doc[pno]
        tops = rows_by_page[pno]
        if not tops:
            continue
        d = page.get_text("dict")
        fx = frame_from_dict(page, d)  # sideways pages: row tops are in the parser's reading frame
        amount_boxes = []
        for b in d.get("blocks", []):
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    txt = sp.get("text", "").strip()
                    if not AMOUNT_TXT_RE.match(txt.replace(" ", "")):
                        continue
                    y0 = (fx(sp["bbox"]) if fx else sp["bbox"])[1]
                    row = next((t for tp, t in tops if abs(tp - y0) < 3.5), None)
                    if row is None:
                        continue
                    key = (re.sub(r"^[A-Z]{6}\+", "", sp.get("font", "")), round(sp.get("size", 0), 1))
                    font_counter[key] += 1
                    font_rows[key].append(row)
                    amount_boxes.append(fitz.Rect(sp["bbox"]))
        if pno < 60 and amount_boxes:
            try:
                for dr in page.get_drawings():
                    fill = dr.get("fill")
                    r = dr.get("rect")
                    if fill and all(c > 0.97 for c in fill[:3]) and r and r.height < 20 and r.width < 160:
                        if any(r.intersects(bx) for bx in amount_boxes):
                            whiteouts += 1
            except Exception:
                pass

    if annot_hits:
        add("Overlay annotations", "fail", "Boxes/text/stamps drawn on top of the statement (can hide or replace "
            "numbers): " + ", ".join(f"{k} x{v}" for k, v in annot_hits.items()), 25)
        fake_reasons.append("objects drawn over the statement")
    if markup:
        add("Reviewer markup", "warn", "Pen/highlight marks present (" + ", ".join(f"{k} x{v}" for k, v in
            markup.items()) + ") - someone annotated this copy; ask for the clean original.", 5)
    if (bank_code == "SCB" and "crystal reports" in creator.lower() and subset_pages and len(subset_pages) >= 3
            and len(subset_pages) == subset_font_pages and len(subset_sigs) == 1):
        # Standard Chartered's Crystal Reports generator embeds Arial twice on EVERY page (genuine SCB_001, SCB_002).
        # Bank-gated (skeptic 2026-11-23): as a general rule a tamperer who edits the same header / narration on
        # every page of a short statement makes exactly this uniform pattern, so other banks keep the warning
        twice = ", ".join(sorted(f"{b} x{n}" for b, n in next(iter(subset_sigs))))
        add("Font subsets", "pass", f"{twice} embedded on every page ({len(subset_pages)} pages, same pattern) - "
                                    "Standard Chartered's Crystal Reports page design, not an edit.")
    elif subset_pages:
        add("Font subsets", "warn", f"Same font embedded twice on page(s) {subset_pages[:8]} - typical when text "
                                    "is added by an editor.", 15)
    try:
        inserted = _inserted_font_values(doc)
    except Exception:
        inserted = []
    if inserted:
        add("Inserted font", "fail", "Values printed in a font used nowhere else in the file (text typed over "
            "the original): " + "; ".join(f"p{p} {f} '{t[:40]}'" for p, f, t in inserted[:5]), 30)
        fake_reasons.append("name / amounts / dates overwritten in a different font")
    total = sum(font_counter.values())
    if total >= 30 and len(font_counter) > 1:
        rare = [(k, c) for k, c in font_counter.items() if c <= max(2, 0.01 * total)]
        if rare:
            rows = sorted({t.idx for k, _ in rare for t in font_rows[k]})[:10]
            desc = ", ".join(f"{k[0]} {k[1]}pt x{c}" for k, c in rare[:4])
            add("Font consistency", "fail", f"Amounts in rows {rows} use a font/size seen almost nowhere else "
                                            f"({desc}) - classic sign of edited numbers.", 30)
        else:
            add("Font consistency", "pass", f"{total} amounts checked; fonts consistent.")
    elif total:
        add("Font consistency", "pass", f"{total} amounts checked; fonts consistent.")
    if 0 < whiteouts <= 10:  # many boxes = zebra-striped row backgrounds, i.e. page design
        add("Whiteout boxes", "warn", f"{whiteouts} small white rectangle(s) drawn over amounts.", 15)
    try:
        hidden = _hidden_text_boxes(doc)
    except Exception:
        hidden = []
    if hidden:
        add("Hidden text", "fail", "White box painted over existing text on page(s) "
            + ", ".join(f"{p} ({n} piece{'s' if n > 1 else ''})" for p, n in hidden[:8])
            + " - something printed on the statement was covered up after it was written.", 30)

    # --- content checks from the reconciliation
    _content_checks(add, recon, parse_conf)

    score = max(0, min(100, 100 - penalty))
    fails = sum(1 for c in checks if c["status"] == "fail")
    if fake_reasons or fails >= 2 or score < 50:
        verdict = "FAKE"
        score = min(score, 20)
    elif fails == 1 or score < 80:
        verdict = "SUSPICIOUS"
    else:
        verdict = "ORIGINAL"
    return {
        "verdict": verdict, "score": score, "checks": checks, "fake_reasons": fake_reasons,
        "pdf_metadata": {"producer": producer or None, "creator": creator or None, "author": author or None,
                         "created": created.isoformat() if created else None,
                         "modified": modified.isoformat() if modified else None,
                         "encrypted": was_encrypted, "signed": signed, "eof_markers": eof},
        "revisions": history,
    }


def _summary_reliable(recon: dict) -> bool:
    ps = recon.get("printed_summary") or {}
    keys = [k for k in ("opening", "debits", "credits", "closing") if k in ps]
    if len(keys) < 3:
        return False
    anchors = []
    if "opening" in ps and recon.get("opening_balance") is not None:
        anchors.append(abs(abs(ps["opening"]) - abs(recon["opening_balance"])) < 1)
    if "closing" in ps and recon.get("closing_balance") is not None:
        anchors.append(abs(abs(ps["closing"]) - abs(recon["closing_balance"])) < 1)
    return any(anchors)


def _content_checks(add, recon: dict, parse_conf: float) -> None:
    mis, tot = recon.get("rows_mismatch", 0), recon.get("rows_total", 0)
    pct = recon.get("verified_pct", 0)
    rows = ", ".join(f"#{m['idx']} {m['date']}" for m in recon.get("mismatch_rows", [])[:6])
    if recon["status"] == "UNVERIFIED":
        add("Balance chain", "warn", "No running balance to verify rows against.", 10)
    elif mis == 0:
        add("Balance chain", "pass", f"All {recon['rows_verified']} rows follow from the previous balance.")
    elif pct >= 97 and mis <= 8:
        # clean parse with a handful of breaks -> the most useful tamper signal we have; reviewer must look
        add("Balance chain", "warn", f"{mis} row(s) don't add up (previous balance +/- amount != printed balance) "
                                     f"while {pct}% reconcile perfectly: {rows}. Check these rows for edited "
                                     "amounts or removed transactions.", max(21, 15 + 3 * mis))
        # (>= 21: a bank never prints a balance that does not follow - one such row keeps the file off ORIGINAL)
    elif pct >= 85:
        add("Balance chain", "warn", f"{mis} of {tot} rows don't reconcile ({pct}% verified). Could be layout "
                                     "parsing limits or edits - review the mismatch rows.", 10)
    else:
        add("Balance chain", "info", f"Only {pct}% of rows could be reconciled - layout not fully understood, so "
                                     "this is not treated as evidence of tampering.")
    psm = recon.get("printed_summary_match")
    if psm is False and _summary_reliable(recon) and pct >= 97:
        add("Printed summary", "fail", "The statement's own printed totals (debits/credits) do not match the sum "
                                       "of its rows, although opening/closing balances do.", 25)
    elif psm is False:
        add("Printed summary", "info", "Could not align the printed summary block with the rows.")
    elif psm:
        add("Printed summary", "pass", "Printed opening/debit/credit/closing totals match the rows.")
