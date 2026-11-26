"""Self-heal layer: try -> prove -> adopt only what the statement itself proves. No AI / ML, no learned state.

  FALLBACK  the normal parse (all hand fixes) runs first. Only when it lacks strong proof does the portfolio run.
  PROOF     score(): chain, printed opening/closing, printed summary + counts, dates in order and inside the printed
            period, no money line left without a row, Dr/Cr column agreement. 'strong' = all of them hold.
  TRY       run(): a bounded portfolio of parameter overrides (date grammar learned from the file's own tokens, other
            layout mode, rows cut at the amount line, money columns from x clusters, explicit row orders), every
            loop under one hard deadline; skipped entirely for a slow default (> 3 s) or a long file (> 150 pages).
  ADOPT     only a strong result that (a) changes the money rows, (b) is not ambiguous between row orders, (c) if it
            cuts rows at the amount line, passes a per-page row-alignment proof (a wrapped narration under the amount
            line would otherwise land on the next row with the chain still 100%), (d) is the ONLY strong reading:
            two strategies that are both strong with different full rows -> neither is adopted, and (e) the whole
            portfolio finished inside its budget (otherwise a disagreement cannot be ruled out).
  ESCALATE  nothing adopted -> default output stays, a text-free line is logged (and a geometry snapshot if asked).
  LOG       append-only, text-free, best-effort JSON lines (see log_event). Nothing is ever read back: no state
            of any earlier file can influence how a later one is parsed.

Env: BSA_SELFHEAL=0 disables (default ON); BSA_SELFHEAL_LOG=<dir> the event log (the web app defaults it to
<data dir>/selfheal_log/; unset / '' / 0 / off = no log, which is the default for tests, scripts and gates); BSA_SELFHEAL_ESC=<dir> geometry snapshots (default off);
BSA_SELFHEAL_FORCE=1 runs the whole portfolio even when the default is strong (tests).
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date

from . import opts as O
from .opts import Grammar, Opts
from .pdf_parser import (PERIOD_HINT_RE, _group_lines, parse_pdf, W)
from .reconcile import reconciliation, resolve
from .utils import MONTHS, find_dates, parse_amount_token

# a default that fails only SOFT checks (date order, Dr/Cr column, period) is kept: the money rows are proven, and a
# strategy could only change which narration / date sits on which row, which nothing in the score can prove
HARD = {"chain", "closing", "opening", "summary", "count", "tail_totals", "money_lines", "summary_kept", "no_rows", "dr_cr_columns", "hints_kept", "dates_spread"}
TIME_FACTOR = 1.2          # the whole portfolio must fit in this many default-parse times ...
TIME_FLOOR = 0.4           # ... but never less than this many seconds of budget
SKIP_SECONDS = 3.0         # a default parse slower than this: no portfolio at all
SKIP_PAGES = 150           # ... nor for a PDF with more pages than this

# ------------------------------------------------------------------ proof
OPEN_RE = re.compile(r"opening\s+bal(?:ance)?\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*(-?[\d,]+\.\d{2})", re.I)
CLOSE_RE = re.compile(r"closing\s+bal(?:ance)?\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*(-?[\d,]+\.\d{2})", re.I)


def _num(s: str) -> float:
    return abs(float(s.replace(",", "")))


def printed_open_close(text: str) -> tuple[float | None, float | None]:
    o, c = OPEN_RE.search(text or ""), CLOSE_RE.search(text or "")
    return (_num(o.group(1)) if o else None, _num(c.group(1)) if c else None)


def printed_period(page1_text: str):
    for ln in (page1_text or "").split("\n")[:60]:
        if not PERIOD_HINT_RE.search(ln):
            continue
        ds = find_dates(ln)
        if len(ds) >= 2 and ds[0][2] < ds[1][2] and (ds[1][2] - ds[0][2]).days <= 800:
            return ds[0][2], ds[1][2]
    return None


TOT_RE = re.compile(r"(?:total|turnover)\D*?([\d,]+\.\d\d)\s+([\d,]+\.\d\d)\s*$", re.I)
CNT_RE = re.compile(r"count\D*?(\d+)\s+(\d+)\s*$", re.I)


def _tail_totals(pr, txns) -> bool | None:
    """Totals / transaction counts printed under the table ('Turnover <a> <b>' + 'Count <n> <m>'): the two columns
    follow the header's Deposits/Credit vs Withdrawals/Debit order. None when not printed."""
    tail = pr.diag.get("tail") or []
    tot = next((m for m in (TOT_RE.search(x) for x in tail) if m), None)
    cnt = next((m for m in (CNT_RE.search(x) for x in tail) if m), None)
    if not (tot or cnt):
        return None
    words = (pr.diag.get("hdr") or {}).get("words", [])
    ic = next((i for i, w in enumerate(words) if w.startswith(("deposit", "credit"))), None)
    idr = next((i for i, w in enumerate(words) if w.startswith(("withdraw", "debit"))), None)
    if ic is None or idr is None:
        return None
    cr_first = ic < idr
    ok = True
    n_cr = sum(1 for t in txns if t.direction == "CR")
    if tot:
        a, b = (float(tot.group(i).replace(",", "")) for i in (1, 2))
        cr, dr = (a, b) if cr_first else (b, a)
        ok &= abs(cr - sum(t.credit for t in txns)) < 1.0 and abs(dr - sum(t.debit for t in txns)) < 1.0
    if cnt:
        a, b = int(cnt.group(1)), int(cnt.group(2))
        c, d = (a, b) if cr_first else (b, a)
        ok &= (c == n_cr and d == len(txns) - n_cr)
    return bool(ok)


def _hint_frac(pr) -> float:
    """Share of rows whose amount carries a printed Dr/Cr cue (debit / credit column, Dr/Cr marker, +/- sign)."""
    if not pr.raw:
        return 0.0
    return sum(1 for r in pr.raw if any(c.col in ("debit", "credit") or c.marker or c.sign for c in r.cands)) / len(pr.raw)


def score(pr, txns, opening, recon, contradictions, baseline: dict | None = None) -> dict:
    """Every check is True / False / None (not applicable). strong = no False and the core checks are True."""
    ck: dict[str, bool | None] = {}
    n = len(txns)
    ver = recon["rows_verified"]
    ck["chain"] = bool(n and recon["rows_mismatch"] == 0 and recon["verified_pct"] == 100.0 and ver >= n - 1)
    p_open, p_close = printed_open_close(pr.page1_text)
    ps0 = pr.printed_summary or {}
    if p_open is None and "opening" in ps0:
        p_open = abs(ps0["opening"])
    last_bal = recon["closing_balance"]
    ck["closing"] = bool(recon["closing_match"]) and (p_close is None or (last_bal is not None
                                                                          and abs(abs(last_bal) - p_close) < 1.0))
    ck["opening"] = None if p_open is None else (opening is not None and abs(abs(opening) - p_open) < 1.0)
    ck["summary"] = recon["printed_summary_match"]            # totals + closing + Dr/Cr counts, None when no summary
    ps = pr.printed_summary or {}
    ck["count"] = (n == int(ps["dr_count"]) + int(ps["cr_count"])) if ("dr_count" in ps and "cr_count" in ps) else None
    ds = [t.date for t in txns]
    back = sum(1 for a, b in zip(ds, ds[1:]) if b < a)
    ck["dates_order"] = bool(ds) and back <= max(1, len(ds) // 200)   # real statements carry the odd back-dated row
    ck["dates_spread"] = None if n < 8 else len(set(ds)) > 1   # >= 8 rows all on ONE date: a mis-read date carried onto every row
    per = printed_period(pr.page1_text)
    ck["dates_period"] = None if per is None or not ds else all(per[0] <= d <= per[1] for d in ds)
    # rows must cover the printed period: a strategy that keeps only the last pages chains 100% on a contiguous block
    ck["period_coverage"] = (None if per is None or not ds else
                             (min(ds) - per[0]).days <= 45 and (per[1] - max(ds)).days <= 45)
    # no page with money-looking lines left without a row
    used: dict[int, list[float]] = {}
    for r in pr.raw:
        used.setdefault(r.page, []).extend(r.tops)
    hp = set(pr.diag.get("hdr_pages") or [])
    stop = pr.diag.get("stop_y", {})
    # on a page with the column titles any money line must be a row; on a page without them (cover / summary pages)
    # only date-led lines with amount + balance count - those are table rows whatever page they sit on
    orphans = [(p, t) for p, t, nc, k in pr.diag.get("money_lines", [])
               if ((not hp or p in hp) or (k == "anchor" and nc >= 2))
               and not (p in stop and t > stop[p]) and not any(abs(t - u) <= 1.6 for u in used.get(p, []))]
    ck["money_lines"] = len(orphans) == 0
    ck["dr_cr_columns"] = contradictions == 0
    ck["tail_totals"] = _tail_totals(pr, txns)
    # date lines left without a row (default / mode strategies; the amount_end strategy has date_gaps instead):
    # a date printed under its row makes every date shift by one row while the chain stays 100%
    ck["dangling_dates"] = None if "date_gaps" in pr.diag else pr.diag.get("dangling", 0) == 0
    ck["date_gaps"] = None if "date_gaps" not in pr.diag else pr.diag["date_gaps"] == 0
    hf = _hint_frac(pr)
    if baseline and hf < baseline.get("hint_frac", 0.0) - 0.05:
        ck["hints_kept"] = False   # evidence monotonicity: a strategy may not blind the Dr/Cr column / marker check
    if baseline and baseline.get("has_summary") and not pr.printed_summary and ck["tail_totals"] is not True:
        ck["summary_kept"] = False   # a strategy may not make a printed summary disappear unless other printed totals confirm it
    core = ck["chain"] and ck["closing"]
    # period_coverage is informational only: real statements have idle months (BANDHAN: 4.8 idle months of 5.7)
    strong = bool(core) and all(v is not False for k, v in ck.items() if k != "period_coverage")
    failed = [k for k, v in ck.items() if v is False]
    return {"strong": strong, "checks": ck, "orphans": len(orphans), "rows": n, "failed": failed,
            "hard_failed": [k for k in failed if k in HARD]}


@dataclass
class Eval:
    opts: Opts
    pr: object = None
    txns: list = None
    opening: float | None = None
    contradictions: int = 0
    recon: dict | None = None
    warnings: list = field(default_factory=list)
    proof: dict | None = None
    seconds: float = 0.0
    sig: tuple = ()


def evaluate(path, doc, opts: Opts, baseline: dict | None = None, pr=None, placeholder: bool = False) -> Eval:
    """parse (unless a parse made with the same layout options is passed in) -> resolve -> reconcile -> score.
    placeholder = the bank prints a placeholder opening balance (Fino): same reconciliation as pipeline."""
    t0 = time.time()
    ev = Eval(opts=opts)
    with O.use(opts):
        ev.pr = pr = pr if pr is not None else parse_pdf(path, doc=doc)
        ev.sig = (tuple((r.date, tuple(c.value for c in r.cands), r.page, tuple(r.narration_parts)) for r in pr.raw),
                  opts.order, opts.row_cut)
        if pr.raw:
            ev.txns, ev.opening, ev.contradictions = resolve(pr.raw, pr.opening_balance, ev.warnings)
            if ev.txns:
                ev.recon = reconciliation(ev.txns, ev.opening, pr.printed_summary, placeholder_opening=placeholder)
                ev.proof = score(pr, ev.txns, ev.opening, ev.recon, ev.contradictions, baseline)
    if ev.proof is None:
        ev.proof = {"strong": False, "checks": {"chain": False}, "failed": ["no_rows"], "hard_failed": ["no_rows"], "rows": 0, "orphans": 0}
    ev.seconds = time.time() - t0
    return ev


# ------------------------------------------------------------------ date grammar learning
_FULL = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
         "november", "december"]
_MONFORMS = set(_FULL) | {m[:3] for m in _FULL} | {"sept"}
MONTH_ALT = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?"
             r"|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_TOK = re.compile(r"\d+|[A-Za-z]+|[^\dA-Za-z\s]|\s+")


def _shape(text: str):
    """(kinds, seps) of the first three date components at the start of text, or None. A component is a digit run or
    a month word; separators are whatever sits between them (may be empty: 'June22')."""
    comps, seps, sep = [], [], ""
    for t in _TOK.findall(text[:40]):
        if t.isdigit():
            kind = "D" + str(len(t)) if len(t) <= 4 else None
        elif t.isalpha():
            kind = "M" if t.lower() in _MONFORMS else None
        else:
            if not comps:
                return None
            sep += " " if t.isspace() else t
            if len(sep) > 3:
                return None
            continue
        if kind is None:
            return None
        if comps:
            seps.append(re.sub(r"\s+", " ", sep))
        comps.append(kind)
        sep = ""
        if len(comps) == 3:
            return tuple(comps), tuple(seps)
    return None


def _build(kinds, seps, order) -> Grammar | None:
    """Regex for one role assignment (order = role of each of the 3 components in reading order)."""
    parts, year2 = [], False
    for k, role in zip(kinds, order):
        if k == "M":
            if role != "m":
                return None
            parts.append("(" + MONTH_ALT + ")")
        else:
            w = int(k[1])
            if role == "y":
                if w not in (2, 4):
                    return None
                year2 = w == 2
                parts.append(r"(\d{%d})" % w)
            else:
                if w > 2:
                    return None
                parts.append(r"(\d{1,2})")
    pat = parts[0]
    for sp, p in zip(seps, parts[1:]):
        pat += "".join(r"\s?" if ch == " " else re.escape(ch) for ch in sp) + p
    pat = r"(?<![A-Za-z\d])" + pat + r"(?!\d)"
    return Grammar(shape="".join(kinds) + "|" + "|".join(seps), pattern=pat, order=tuple(order),
                   month_name=("M" in kinds), year2=year2)


def _first_dates(lines_all, g: Grammar):
    rx, out = g.compiled(), []
    from .utils import grammar_date
    for n, ln in enumerate(lines_all):
        if n % 256 == 0:
            O.check_deadline()
        m = rx.match(ln.text)
        if m:
            d = grammar_date(g, m.groups())
            out.append(d)
    return out


def learn_grammars(pages_lines, page1_text: str) -> tuple[list[Grammar], list[dict]]:
    """Date grammars the document's own line starts support, for lines the built-in patterns do not date. A grammar
    is kept only when its hits (a) are valid dates, (b) are monotone (>= 95% one direction), (c) sit inside the
    printed period. If more than one role assignment survives (day/month ambiguity) NONE is offered."""
    lines_all = [ln for pl in pages_lines for ln in pl]
    shapes: dict = {}
    for n, ln in enumerate(lines_all):
        if n % 256 == 0:
            O.check_deadline()
        ds = find_dates(ln.text)
        if ds and ds[0][0] <= 8:
            continue  # the built-in grammar already dates this line
        s = _shape(ln.text)
        if s:
            shapes[s] = shapes.get(s, 0) + 1
    good, notes = [], []
    per = printed_period(page1_text)
    for (kinds, seps), cnt in sorted(shapes.items(), key=lambda kv: -kv[1]):
        if cnt < 4:
            continue
        passing = []
        for order in set(itertools.permutations("dmy")):
            O.check_deadline()
            g = _build(kinds, seps, order)
            if g is None:
                continue
            ds = _first_dates(lines_all, g)
            hits = [d for d in ds]
            if len(hits) < max(4, int(0.8 * cnt)) or any(d is None for d in hits):
                continue
            up = sum(b > a for a, b in zip(hits, hits[1:]))
            dn = sum(b < a for a, b in zip(hits, hits[1:]))
            if up + dn and max(up, dn) < 0.95 * (up + dn):
                continue
            with O.use(Opts(grammars=(g,))):
                per2 = printed_period(page1_text) or per
            if per2 is not None and sum(per2[0] <= d <= per2[1] for d in hits) < 0.95 * len(hits):
                continue
            passing.append(g)
        if len(passing) == 1:
            good.append(passing[0])
        elif len(passing) > 1:
            notes.append({"shape": "".join(kinds) + "|" + "|".join(seps), "hits": cnt, "ambiguous_roles":
                          sorted("".join(g.order) for g in passing)})
    return good, notes




# ------------------------------------------------------------------ fingerprint (log only)
_HDR_VOCAB = {"date", "value", "description", "narration", "particulars", "amount", "debit", "credit", "withdrawal",
              "withdrawals", "deposit", "deposits", "balance", "dr", "cr", "transaction", "details", "ref", "cheque",
              "chq", "remarks", "closing", "type"}


def _header_guess(pages_lines) -> dict:
    """Column-title line for documents whose header the parser did not recognise (BANDHAN 'Amount Dr / Cr Balance'):
    the first line with >= 4 words from the usual title vocabulary."""
    for lines in pages_lines:
        for ln in lines:
            ws = [w for w in ln.words if re.sub(r"[^a-z]", "", w.t.lower()) in _HDR_VOCAB]
            if len(ws) >= 4:
                return {"words": [w.t.lower() for w in ln.words], "x": [round(w.x0 / 10) * 10 for w in ln.words]}
    return {}


def _clean_shape(s: str) -> str:
    return "".join(ch if ch in "DM0123456789| -/.:,'" else "?" for ch in s)[:24]


def fingerprint(pr, grammars: tuple = (), hdr: dict | None = None) -> dict:
    """Layout shape of the file for the LOG only: standard column-title words (nothing else of the header line),
    x bands, date-grammar shapes. Never looked up by anything."""
    hdr = pr.diag.get("hdr") or hdr or {}
    words = [w for w in (re.sub(r"[^a-z]", "", x) for x in hdr.get("words", [])) if w in _HDR_VOCAB]
    xb = [int(x) for x in hdr.get("x", [])[:16] if isinstance(x, (int, float))]
    key = hashlib.sha1(json.dumps([words, xb], sort_keys=True).encode()).hexdigest()[:12]
    return {"key": key, "header": words[:12], "xbands": xb, "grammar": [_clean_shape(g.shape) for g in grammars][:4]}


# ------------------------------------------------------------------ event log (write-only)
def log_dir() -> str | None:
    """Directory of the event log, or None = no logging. OFF unless BSA_SELFHEAL_LOG names a directory (the web app
    sets it to <data dir>/selfheal_log in bsa/config.py, so engine tests, batch scripts and gates never write)."""
    v = os.environ.get("BSA_SELFHEAL_LOG")
    if v is None or v.strip().lower() in ("", "0", "off", "none", "false"):
        return None
    return v


def log_event(kind: str, fp: dict, info: dict, proof: dict | None) -> None:
    """Append one JSON line: timestamp, kind (win | escalation), layout fingerprint shape, strategy name, reason codes
    and a proof summary (check names + row counts). No text of the document except the standard column-title words
    in the fingerprint. Best effort: never raises, one O_APPEND write, so concurrent uploads cannot corrupt it.
    Nothing reads this file; it cannot influence any analysis."""
    try:
        d = log_dir()
        if not d:
            return
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "fp": fp, "winner": info.get("winner"),
               "reasons": info.get("reasons", [])[:8], "tried": [t.get("name") for t in info.get("tried", [])][:24],
               "time_cap_hit": bool(info.get("time_cap_hit")), "seconds": info.get("seconds_total"),
               "rows": (proof or {}).get("rows"), "failed": list((proof or {}).get("failed", []))[:12]}
        line = (json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")[:8000]
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, time.strftime("events-%Y%m.jsonl")), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        pass


# ------------------------------------------------------------------ escalation snapshot (opt-in, text-free)
def _sha8(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:8]


def _cls(t: str) -> str:
    if find_dates(t):
        return "D"
    p = parse_amount_token(t)
    if p is not None:
        return "A" if p[2] else "I"
    if t.lower() in _MONFORMS:
        return "M"
    return "T" + str(min(len(t) // 4, 5))


def snapshot(path: str, doc, why: list, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    pages = []
    for pno in range(min(doc.page_count, 4)):
        from .pdf_parser import _page_words
        lines = _group_lines(pno, _page_words(doc[pno]))
        pages.append([{"y": round(ln.top), "t": [[_cls(w.t), round(w.x0)] for w in ln.words]} for ln in lines])
    name = os.path.join(out_dir, _sha8(path) + ".json")
    fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump({"why": why, "pages": pages}, f)
    os.replace(tmp, name)
    return name


# ------------------------------------------------------------------ the portfolio
@dataclass
class Healed:
    ev: Eval
    info: dict
    strong: list = field(default_factory=list)   # every strong + eligible Eval found (several only when force_all)


ORDERS = ("printed", "reversed", "sort_reversed", "sort_printed")   # closest to the default first


def _parse_strategies(default_mode: str, grams: list[Grammar], cache) -> list[Opts]:
    """Parse-level overrides (each also gets the explicit row orders), nearest to the default first."""
    other = "centered" if default_mode == "below" else "below"
    G = tuple(grams)
    out = [Opts(name="grammar", grammars=(g,), cache=cache) for g in grams]
    out += [Opts(name=f"mode={other}", force_mode=other, cache=cache),
            Opts(name="amount_end", row_cut="amount_end", cache=cache),
            Opts(name="xcluster", col_roles="xcluster", cache=cache)]
    if G:
        out += [Opts(name="grammar+amount_end", grammars=G, row_cut="amount_end", cache=cache),
                Opts(name=f"grammar+mode={other}", grammars=G, force_mode=other, cache=cache)]
    return out


def _with_order(o: Opts, order: str) -> Opts:
    return Opts(name=f"{o.name}+order:{order}", grammars=o.grammars, force_mode=o.force_mode, row_cut=o.row_cut,
                order=order, col_roles=o.col_roles, cache=o.cache)


def _full_rows(ev: Eval):
    return [(t.date, t.debit, t.credit, t.balance, t.narration) for t in (ev.txns or [])]


def _money_rows(ev: Eval):
    return [(t.date, t.debit, t.credit, t.balance) for t in (ev.txns or [])]


def run(path: str, doc, force_all: bool = False) -> Healed:
    """Default parse -> (only if it lacks strong proof) the portfolio -> adopt the single strong, eligible,
    unambiguous, alignment-proven result. Never raises for a strategy failure: a strategy that throws is just a
    strategy that did not prove. Everything else keeps the default output."""
    t_all = time.monotonic()
    cache: dict = {}
    base = Opts(cache=cache)
    d0 = evaluate(path, doc, base)
    info = {"enabled": True, "tried": [{"name": "default", "strong": d0.proof["strong"], "rows": d0.proof["rows"],
                                        "failed": d0.proof["failed"], "sec": round(d0.seconds, 3)}],
            "winner": "default", "default_strong": d0.proof["strong"], "proof": d0.proof}
    force_all = force_all or os.environ.get("BSA_SELFHEAL_FORCE") == "1"
    if os.environ.get("BSA_SELFHEAL") == "0":
        info["enabled"] = False
        return Healed(d0, info, [])
    if d0.proof["strong"] and not force_all:
        return Healed(d0, info, [d0])
    if not d0.proof["strong"] and not d0.proof["hard_failed"] and d0.proof["rows"] and not force_all:
        info["soft_only"] = d0.proof["failed"]   # money rows proven, only soft checks fail -> default kept untouched
        return Healed(d0, info, [])
    if d0.pr.text_chars < 100:   # scanned / image-only: nothing to heal, the verdict path handles it
        info["no_text"] = True
        return Healed(d0, info, [])
    reasons: list[str] = []
    info["reasons"] = reasons
    fp = fingerprint(d0.pr, (), {})

    def escalate(why: str) -> Healed:
        reasons.append(why)
        info["escalated"] = True
        info["seconds_total"] = round(time.monotonic() - t_all, 3)
        log_event("escalation", fp, info, d0.proof)
        esc = os.environ.get("BSA_SELFHEAL_ESC")
        if esc:
            try:
                info["snapshot"] = snapshot(path, doc, d0.proof["failed"], esc)
            except Exception:
                pass
        return Healed(d0, info, [])

    # cost guard: a slow default or a long file never gets the portfolio
    if not force_all and (d0.seconds > SKIP_SECONDS or doc.page_count > SKIP_PAGES):
        info["skipped_cost"] = True
        return escalate("skipped: slow default or long file")
    try:
        from .banks import detect_bank
        placeholder = detect_bank(d0.pr.header_text, d0.pr.page1_text, "", doc)[0] == "FINO"
    except Exception:
        placeholder = False
    if placeholder and d0.pr.raw:   # same reconciliation as the pipeline: a printed placeholder opening is dropped
        d0 = evaluate(path, doc, base, None, d0.pr, True)
        info["tried"][0].update(strong=d0.proof["strong"], failed=d0.proof["failed"])
        info["default_strong"], info["proof"] = d0.proof["strong"], d0.proof
        if d0.proof["strong"] and not force_all:
            return Healed(d0, info, [d0])
    budget = max(TIME_FLOOR, TIME_FACTOR * d0.seconds)
    t_dead = None if force_all else time.monotonic() + budget
    baseline = {"has_summary": bool(d0.pr.printed_summary), "hint_frac": _hint_frac(d0.pr)}

    seen = {d0.sig}
    winners: list[Eval] = []

    def attempt(o: Opts, pr=None) -> Eval | None:
        try:
            ev = evaluate(path, doc, o, baseline, pr, placeholder)
        except Exception as e:  # a strategy that crashes simply did not prove
            info["tried"].append({"name": o.name, "error": type(e).__name__})
            return None
        if ev.sig in seen:
            info["tried"].append({"name": o.name, "skipped": "same rows as an earlier strategy"})
            return ev
        seen.add(ev.sig)
        rec = {"name": o.name, "strong": ev.proof["strong"], "rows": ev.proof["rows"], "failed": ev.proof["failed"],
               "sec": round(ev.seconds, 3)}
        info["tried"].append(rec)
        if ev.proof["strong"]:
            why = _ineligible(d0, ev)
            if why:
                rec["rejected"] = why
            elif o.row_cut == "amount_end" and not ev.pr.diag.get("alignment_ok"):
                # rows cut at the amount line: a wrapped narration printed UNDER its amount line lands on the next
                # row while the chain stays 100% - adopted only when every table page proves its row alignment
                rec["rejected"] = "amount_end: row alignment not proven on every page"
                rec["alignment"] = [{"page": a["page"], "proven": a["proven"], "why": a["why"]}
                                    for a in ev.pr.diag.get("alignment", [])][:12]
                reasons.append("amount_end alignment unproven")
            elif o.force_mode and not (ev.pr.page_proof and all(p["proven"] for p in ev.pr.page_proof)):
                rec["rejected"] = "mode override: row alignment not proven on every page"
            else:
                amb = _ambiguous(path, doc, o, ev, baseline, d0, placeholder)
                if amb:
                    rec["rejected"] = "ambiguous: " + amb
                else:
                    winners.append(ev)
        return ev

    def distinct() -> int:
        return len({repr(_full_rows(w)) for w in winners})

    def with_orders(o: Opts, ev: Eval | None):
        """The same parse under each explicit row order - only when it has rows and a chain / opening / closing problem."""
        if ev is None or not ev.pr.raw:
            return
        if not ({"chain", "opening", "closing", "dr_cr_columns"} & set(ev.proof["hard_failed"])):
            return
        for od in ORDERS:
            if distinct() > 1 and not force_all:
                return
            O.check_deadline()
            attempt(_with_order(o, od), pr=ev.pr)

    capped = False
    try:
        with O.deadline(t_dead):
            O.check_deadline()
            pages_lines = [_group_lines(p, cache[p][0]) for p in sorted(cache)]
            with O.use(Opts(cache=cache)):
                grams, gnotes = learn_grammars(pages_lines, d0.pr.page1_text)
            info["grammars_learned"] = [_clean_shape(g.shape) for g in grams]
            if gnotes:
                info["grammar_rejected"] = [{"shape": _clean_shape(n["shape"]), "hits": n["hits"], "ambiguous_roles": n["ambiguous_roles"]}
                                         for n in gnotes][:6]
            fp = fingerprint(d0.pr, tuple(grams), _header_guess(pages_lines))
            info["fingerprint"] = fp["key"]
            with_orders(base, d0)
            for o in _parse_strategies(d0.pr.diag.get("default_mode", "below"), grams, cache):
                if distinct() > 1 and not force_all:
                    break   # two different strong readings: already a disagreement
                O.check_deadline()
                ev = attempt(o)
                with_orders(o, ev)
    except O.DeadlineExceeded:
        capped = True
        info["time_cap_hit"] = True
    info["seconds_total"] = round(time.monotonic() - t_all, 3)
    info["budget_s"] = round(budget, 2)
    if d0.proof["strong"]:  # force_all run: the default was strong, keep it
        info["winners_also_strong"] = [w.opts.name for w in winners]
        return Healed(d0, info, [d0] + winners)
    if capped and winners:
        # not every strategy was tried: a second strong reading cannot be ruled out
        return escalate("time cap hit before the portfolio finished; strong result not adopted")
    if distinct() > 1:
        info["disagreement"] = [w.opts.name for w in winners]
        return escalate("strategies disagree: strong but different rows")
    if winners:
        w = winners[0]
        info["winner"] = w.opts.name
        info["winner_proof"] = w.proof["checks"]
        info["winners_also_strong"] = [x.opts.name for x in winners[1:]]
        log_event("win", fp, info, w.proof)
        return Healed(w, info, winners)
    return escalate("no strategy reached strong proof")


def _ineligible(d0: Eval, ev: Eval) -> str | None:
    """A strong result is still not adopted when
    - its money rows (date, debit, credit, balance) equal the default's: only narration segmentation would change and
      that is not proven by anything the score measures (SCB / AU: narrations drifted, summary evidence vanished);
    - it keeps clearly fewer rows than a default that already chained >= 90% (contiguous-block truncation)."""
    if d0.txns and _money_rows(ev) == _money_rows(d0):
        return "same money rows as default - narration split unproven, default kept"
    if d0.recon and d0.recon["verified_pct"] >= 90 and len(ev.txns) < 0.9 * len(d0.txns):
        return "fewer rows than a default that already chains >= 90%"
    return None


def _ambiguous(path, doc, o: Opts, ev: Eval, baseline, d0: Eval, placeholder: bool = False) -> str | None:
    """Same parse, other explicit row orders: if one of them is ALSO strong and eligible but puts different money rows
    on the page (equal amounts chain in several orders), the proof cannot tell which is right -> nothing is adopted."""
    base = Opts(name=o.name, grammars=o.grammars, force_mode=o.force_mode, row_cut=o.row_cut, col_roles=o.col_roles,
                cache=o.cache)
    mine = _money_rows(ev)
    for od in ORDERS + (None,):
        if od == o.order:
            continue
        O.check_deadline()
        alt = _with_order(base, od) if od else base
        try:
            e2 = evaluate(path, doc, alt, baseline, ev.pr, placeholder)
        except Exception:
            continue
        if e2.proof["strong"] and not _ineligible(d0, e2) and _money_rows(e2) != mine:
            return f"order '{od}' is also strong with different rows"
    return None
