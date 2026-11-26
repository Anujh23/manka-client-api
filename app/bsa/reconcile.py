"""Balance-chain resolver.

For every row: delta = balance - previous balance. The candidate amount equal to |delta| is the
transaction amount and the sign of delta is the direction. A row is `verified` when that holds.
Rows that don't add up are where either the parser mis-read something or someone edited a number.
"""
from __future__ import annotations

from contextvars import ContextVar

from .models import Cand, RawTxn, Txn
from .opts import OPTS, check_deadline
from .utils import norm

CR_KW = ("/CR/", " CR ", "UPI/CR", "NEFT CR", "IMPS CR", "BY TRANSFER", "BY CLG", "BY CASH", "DEP TFR", "DEPOSIT",
         "CREDIT", "RECEIVED", "RECD", "INWARD", "INW", "SALARY", "REFUND", "REVERSAL", "INT.PD", "INTEREST",
         "CASHBACK", "FROM:", "TRF FROM", "TRANSFER FROM", "-CR-", "MMT/IMPS", "RECD:IMPS")
DR_KW = ("/DR/", " DR ", "UPI/DR", "TO TRANSFER", "WDL", "WITHDRAWAL", "ATM", "POS ", "PURCHASE", "DEBIT", "PAID",
         "CHG", "CHARGE", "FEE", "EMI", "ACH D", "NACH", "TO CLG", "TRF TO", "TRANSFER TO", "-DR-", "BILL")


def _hint(c: Cand | None, r: RawTxn) -> str | None:
    if c is not None:
        if c.marker:
            return c.marker
        if c.sign == "+":
            return "CR"
        if c.sign == "-":
            return "DR"
        if c.col == "debit":
            return "DR"
        if c.col == "credit":
            return "CR"
    return r.dir_hint


def _guess(narr: str) -> str:
    u = f" {norm(narr)} "
    cr = sum(1 for k in CR_KW if k in u)
    dr = sum(1 for k in DR_KW if k in u)
    return "CR" if cr > dr else "DR"


def _pick(amts: list[Cand]) -> Cand | None:
    if not amts:
        return None
    for c in reversed(amts):
        if c.col in ("debit", "credit", "amount") or c.marker or c.sign:
            return c
    return amts[-1]


def _split(r: RawTxn):
    cands = r.cands
    bal_c, amts = None, []
    if len(cands) >= 2:
        bal_c = cands[-1]
        amts = [c for c in cands[:-1] if c.value > 0]
    elif len(cands) == 1:
        c = cands[0]
        if c.col == "balance":
            bal_c = c
        elif c.value > 0:
            amts = [c]
    balance = None
    if bal_c is not None:
        balance = -bal_c.value if (bal_c.marker == "DR" or bal_c.sign == "-") else bal_c.value
    return balance, amts


def _fits(r: RawTxn, cur: float | None) -> bool:
    if cur is None:
        return False
    b, amts = _split(r)
    return b is not None and any(abs(c.value - abs(b - cur)) <= _TOL.get() for c in amts) and abs(b - cur) > 0


def _fit_count(block: list[RawTxn], cur: float | None) -> int:
    n = 0
    for r in block:
        if _fits(r, cur):
            n += 1
        b, _ = _split(r)
        if b is not None:
            cur = b
    return n


def _chain_reorder(raws: list[RawTxn], opening: float | None) -> tuple[list[RawTxn], int]:
    """Within one date, rows are sometimes printed out of order (AA-rendered PDFs, some CBS exports).
    Greedily re-order a same-date block so each row's balance follows from the previous one."""
    out: list[RawTxn] = []
    prev = opening
    moved = 0
    i = 0
    while i < len(raws):
        j = i
        while j < len(raws) and raws[j].date == raws[i].date:
            j += 1
        block = raws[i:j]
        chosen = block
        if len(block) > 1 and prev is not None and _fit_count(block, prev) < len(block):
            remaining, ordered, cur = list(block), [], prev
            while remaining:
                pick = next((r for r in remaining if _fits(r, cur)), None)
                if pick is None:
                    break
                ordered.append(pick)
                remaining.remove(pick)
                cur = _split(pick)[0]
            cand = ordered + remaining
            if _fit_count(cand, prev) > _fit_count(block, prev):
                chosen = cand
                moved += 1
        out.extend(chosen)
        for r in reversed(chosen):
            b, _ = _split(r)
            if b is not None:
                prev = b
                break
        else:
            if prev is None:
                pass
        if prev is None:
            b, amts = _split(chosen[-1])
            prev = b
        i = j
    return out, moved


def _last_balance(rows: list[RawTxn], start: float | None) -> float | None:
    cur = start
    for r in rows:
        b, _ = _split(r)
        if b is not None:
            cur = b
    return cur


def _window_repair(raws: list[RawTxn], opening: float | None, width: int = 5) -> tuple[list[RawTxn], int]:
    """At each chain break, try every order of the few rows around it (dates may differ: AA data is sometimes
    stored out of order across days). Accept only if the whole window AND the row after it then chain."""
    from itertools import permutations
    raws = list(raws)
    fixed = 0
    i, guard = 0, 0
    prev = opening
    while i < len(raws) and guard < 500:
        check_deadline()  # self-heal strategies only; the normal parse has no deadline
        r = raws[i]
        if prev is None or _split(r)[0] is None or _fits(r, prev) or not _split(r)[1]:
            prev = _last_balance([r], prev)
            i += 1
            continue
        guard += 1
        best = None
        for lo in range(max(0, i - 2), i + 1):
            check_deadline()
            hi = min(len(raws), lo + width)
            win = raws[lo:hi]
            if len(win) < 2:
                continue
            p0 = _last_balance(raws[:lo], opening)
            tail = raws[hi:hi + 1]
            for perm in permutations(win):
                perm = list(perm)
                if perm == win:
                    continue
                if p0 is None:  # no opening balance printed: the first row of the window defines it
                    if _split(perm[0])[0] is not None and \
                            _fit_count(perm[1:] + tail, _split(perm[0])[0]) == len(perm) - 1 + len(tail):
                        best = (lo, hi, perm)
                        break
                    continue
                if _fit_count(perm + tail, p0) == len(perm) + len(tail):
                    best = (lo, hi, perm)
                    break
            if best:
                break
        if not best:
            # a single row displaced further away (AA data): pull the nearest row (±3 days, ±60 rows) that fits here,
            # if that raises the chain fit of the whole neighbourhood
            lo, hi = max(0, i - 60), min(len(raws), i + 61)
            for j in sorted(range(lo, hi), key=lambda j: abs(j - i)):
                if j == i or abs((raws[j].date - r.date).days) > 3 or not _fits(raws[j], prev):
                    continue
                trial = list(raws)
                row = trial.pop(j)
                trial.insert(i if j > i else i - 1, row)
                a, b = max(0, min(i, j) - 1), min(len(raws), max(i, j) + 2)
                if _fit_count(trial[a:b], _last_balance(trial[:a], opening)) > \
                        _fit_count(raws[a:b], _last_balance(raws[:a], opening)):
                    best = (None, None, trial)
                    break
        if not best:
            best = _block_move(raws, i, prev, opening)
        if best:
            lo, hi, perm = best
            if lo is None:  # whole list replaced (row/block moved); hi = first index that may need re-checking
                raws = perm
                hi = i + 1 if hi is None else hi
            else:
                raws[lo:hi] = perm
            fixed += 1
            prev = _last_balance(raws[:hi], opening)
            i = hi
        else:
            prev = _last_balance([r], prev)
            i += 1
    return raws, fixed


def _block_move(raws: list[RawTxn], i: int, prev: float, opening: float | None, max_k: int = 8,
                reach: int = 300, days: int = 7):
    """Break at row i. If rows i..i+k-1 chain among themselves, removing them heals the chain here, and there is a
    spot (±reach rows, ±days) where the balance before equals the block's entry balance and the row after fits the
    block's exit balance -> move the block there. Typical: UPI debit + reversal pairs, ₹1 penny-drop runs that the
    AA data stores away from their real position."""
    n = len(raws)
    for k in range(1, max_k + 1):
        if i + k >= n:
            break
        block = raws[i:i + k]
        if any(_split(x)[0] is None for x in block):
            break
        if k > 1 and not _fits(block[-1], _split(block[-2])[0]):
            break  # block no longer chains internally; longer blocks won't either
        if not _fits(raws[i + k], prev):
            continue
        b0, amts0 = _split(block[0])
        entries = {round(b0 + s * c.value, 2) for c in amts0 for s in (1, -1)}
        entries = {e for e in entries if _fits(block[0], e)}
        exit_b = _split(block[-1])[0]
        rest = raws[:i] + raws[i + k:]
        best_p = None
        for p in sorted(range(max(1, i - reach), min(len(rest), i + reach) + 1), key=lambda p: abs(p - i)):
            if p == i:
                continue
            before = _split(rest[p - 1])[0]
            if before is None or round(before, 2) not in entries:
                continue
            if abs((rest[p - 1].date - block[0].date).days) > days:
                continue
            if p < len(rest) and not _fits(rest[p], exit_b):
                continue
            best_p = p
            break
        if best_p is not None:
            return None, min(i, best_p), rest[:best_p] + block + rest[best_p:]
    return None


def _fix_unsigned_negatives(raws: list[RawTxn], opening: float | None) -> int:
    """Some AA renderers drop the minus sign on overdrawn balances. Flip a balance to negative only when the row
    fits with -balance and the following row also fits starting from -balance (proved from both sides)."""
    prev, n = opening, 0
    for k, r in enumerate(raws):
        b, amts = _split(r)
        if b is None:
            continue
        if prev is not None and b > 0 and amts and not _fits(r, prev):
            nb = -b
            if any(abs(c.value - abs(nb - prev)) <= _TOL.get() for c in amts):
                nxt = next((x for x in raws[k + 1:] if _split(x)[0] is not None), None)
                if nxt is None or _fits(nxt, nb):
                    r.cands[-1].sign = "-"
                    r.cands[-1].marker = None
                    r.sign_fixed = True
                    b, n = nb, n + 1
        prev = b
    return n


def _explain_breaks(raws: list[RawTxn], opening: float | None) -> dict[int, tuple]:
    """Rows that don't fit one-by-one but are proven by arithmetic:
    - shared balance: 2-4 consecutive rows print the same balance (txn + its charge, debit + reversal); the
      signed sum of their amounts equals the balance move -> each row gets the direction that makes the sum work.
    - abs balance in source: prev - amount == -balance (overdraft) but the source prints +balance and the next row
      continues from +balance. Amount is proven; the printed balance is the bank/AA's abs() quirk.
    Returns {id(raw): (direction, tag, expected_balance)}."""
    from itertools import product
    out: dict[int, tuple] = {}
    prev = opening
    i = 0
    while i < len(raws):
        r = raws[i]
        b, amts = _split(r)
        if prev is None or b is None or not amts or _fits(r, prev):
            prev = b if b is not None else prev
            i += 1
            continue
        k = i + 1
        while k < len(raws) and k - i < 4 and _split(raws[k])[0] is not None and \
                abs(_split(raws[k])[0] - b) < 0.005 and _split(raws[k])[1]:
            k += 1
        group = raws[i:k]
        delta = round(b - prev, 2)
        done = False
        if len(group) > 1:
            vals = [_pick(_split(g)[1]).value for g in group]
            pref = [(_hint(_pick(_split(g)[1]), g) or _guess(" ".join(g.narration_parts))) for g in group]
            best = None
            for signs in product((1, -1), repeat=len(group)):
                if abs(sum(s * v for s, v in zip(signs, vals)) - delta) <= _TOL.get():
                    agree = sum(1 for s, p in zip(signs, pref) if (s > 0) == (p == "CR"))
                    if best is None or agree > best[0]:
                        best = (agree, signs)
            if best:
                for g, s in zip(group, best[1]):
                    out[id(g)] = ("CR" if s > 0 else "DR", "shared_balance", None)
                prev, i, done = b, k, True
        if not done and b > 0:
            c = next((c for c in amts if abs(prev - c.value + b) <= _TOL.get()), None)
            nxt = raws[i + 1] if i + 1 < len(raws) else None
            if c is not None and nxt is not None and _fits(nxt, b):
                out[id(r)] = ("DR", "abs_balance_in_source", round(-b, 2))
                prev, i, done = b, i + 1, True
        if not done:
            prev = b
            i += 1
    return out


def _same_row(a: RawTxn, b: RawTxn) -> bool:
    return (a.date == b.date and " ".join(a.narration_parts) == " ".join(b.narration_parts)
            and [c.value for c in a.cands] == [c.value for c in b.cands])


# amount-vs-balance tolerance. 1 paisa normally; 10 paise for statements that print amounts to one decimal
# ('26.9', '14.2' - Bank of Maharashtra), where the bank's own rounding leaves 0.1 gaps in the chain
_TOL: ContextVar[float] = ContextVar("bsa_tol", default=0.011)


def _hint_contradictions(rows: list[RawTxn], opening: float | None) -> int:
    """Rows whose chain direction disagrees with the printed column / Dr-Cr marker of the matching amount."""
    cur, n = opening, 0
    for r in rows:
        b, amts = _split(r)
        if b is not None and cur is not None and abs(b - cur) > 0:
            d = "CR" if b > cur else "DR"
            c = next((c for c in amts if abs(c.value - abs(b - cur)) <= _TOL.get()), None)
            h = _hint(c, r) if c is not None else None
            if h and h != d:
                n += 1
        if b is not None:
            cur = b
    return n


def _amount_precision_tol(raws: list[RawTxn]) -> float:
    vals = [c.value for r in raws for c in r.cands if c.has_dec]
    two = sum(1 for v in vals if round(v * 100) % 10 != 0)
    one = sum(1 for v in vals if round(v * 10) % 10 != 0)
    return 0.11 if (two == 0 and one >= 20) else 0.011


def resolve(raws: list[RawTxn], opening: float | None, warnings: list[str]):
    _TOL.set(_amount_precision_tol(raws))
    # Pick the row order that best satisfies the balance chain: as printed, reversed, or date-sorted.
    options = [("as printed", raws)]
    if len(raws) >= 2 and raws[0].date > raws[-1].date:
        options.insert(0, ("reverse chronological - re-ordered oldest first", raws[::-1]))
    base = options[0][1]
    if any(b.date < a.date for a, b in zip(base, base[1:])):
        options.append(("rows not in date order - sorted by date", sorted(base, key=lambda r: r.date)))
    if len(raws) >= 2 and raws[0].date > raws[-1].date:
        # newest day first but each day's rows printed oldest first (ESAF): flip the days, keep the same-day order.
        # Same-day greedy re-ordering can't recover this when a balance repeats within the day (25 -> x -> 25 -> y).
        # Only wins if it chains strictly more rows than every other order.
        options.append(("newest date first, same-day rows oldest first - re-ordered by date",
                        sorted(raws, key=lambda r: r.date)))
    forced = OPTS.get().order
    if forced:
        # self-heal portfolio: exactly one named order (the proof, not the chain-fit count, decides between orders)
        pick = {"printed": list(raws), "reversed": raws[::-1],
                "sort_printed": sorted(raws, key=lambda r: r.date),          # stable: each day keeps its printed order
                "sort_reversed": sorted(raws[::-1], key=lambda r: r.date)}[forced]
        options = [("self-heal order: " + forced, pick)]
    best = None
    for label, rows in options:
        rr, moved = _chain_reorder(rows, opening)
        score = _fit_count(rr, opening)
        if label.startswith("newest date first, same-day") and best is not None:
            # skeptic 2026-11-20: on a fully reversed file with equal same-day amounts this order (days ascending,
            # each day newest first - no bank prints that) can win by luck and flip a DR into a 'verified' CR.
            # It may not make more rows contradict their Debit/Credit column than the order it beats, and a
            # 1-row lead on a still-broken chain is not enough: it needs the whole chain or a 2-row lead
            if score > best[0] and _hint_contradictions(rr, opening) > _hint_contradictions(best[2], opening):
                continue
            chainable = sum(1 for r in rr if _split(r)[0] is not None and _split(r)[1]) - (opening is None)
            if score < best[0] + 2 and score < chainable:
                continue
        if best is None or score > best[0]:
            best = (score, label, rr, moved)
    _, label, raws, moved = best
    if label != "as printed":
        warnings.append(f"Statement order: {label}.")
    if moved:
        warnings.append(f"Re-ordered same-day rows on {moved} date(s) to follow the balance chain.")
    flipped = _fix_unsigned_negatives(raws, opening)
    if forced and _fit_count(raws, opening) < 0.85 * len(raws):
        repaired = 0  # self-heal: an order that mostly fails to chain is not worth the (slow) window repair
    else:
        raws, repaired = _window_repair(raws, opening)
    flipped += _fix_unsigned_negatives(raws, opening)
    if repaired:
        warnings.append(f"Re-ordered rows at {repaired} place(s) (across dates) where the printed order broke the "
                        "balance chain; every amount and balance is unchanged.")
    if flipped:
        warnings.append(f"{flipped} overdrawn balance(s) printed without a minus sign; sign restored because the "
                        "rows before and after prove it.")
    explained = _explain_breaks(raws, opening)
    n_shared = sum(1 for v in explained.values() if v[1] == "shared_balance")
    n_abs = sum(1 for v in explained.values() if v[1] == "abs_balance_in_source")
    if n_shared:
        warnings.append(f"{n_shared} rows share one printed balance with a neighbour (transaction + its charge, "
                        "or debit + reversal); verified as a group.")
    if n_abs:
        warnings.append(f"{n_abs} row(s) took the account overdrawn but the source prints the balance without the "
                        "minus sign and continues from it - amounts proven, printed balance is a source quirk.")
    txns: list[Txn] = []
    prev = opening
    inferred_opening = None
    contradictions = 0
    dups: list[str] = []
    last_raw = None
    for r in raws:
        narr = " ".join(r.narration_parts).strip()
        balance, amts = _split(r)
        if (last_raw is not None and balance is not None and prev is not None and abs(balance - prev) < 0.005
                and _same_row(r, last_raw)):
            dups.append(f"{r.date.isoformat()} {narr[:40]}")
            continue  # identical row printed twice, balance did not move -> duplicate in source
        last_raw = r
        tags: list[str] = ["balance_sign_restored"] if r.sign_fixed else []
        verified = None
        expected = None
        if id(r) in explained and amts:
            direction, tag, expected = explained[id(r)]
            amount, verified = _pick(amts).value, True
            tags.append(tag)
        elif balance is not None and prev is not None:
            delta = round(balance - prev, 2)
            match = [c for c in amts if abs(c.value - abs(delta)) <= _TOL.get()]
            if match and abs(delta) > 0:
                c = match[-1]
                amount, direction, verified = c.value, ("CR" if delta > 0 else "DR"), True
                h = _hint(c, r)
                if c.sign == "-" and not c.marker and (c.col, direction) in (("debit", "CR"), ("credit", "DR")):
                    # contra entry (e.g. HDFC card reversal): printed as a negative number in the opposite
                    # column; the chain proves the direction, the bank's summary totals sum that column signed
                    tags.append(f"negative_in_{c.col}_col")
                elif h and h != direction:
                    contradictions += 1
            elif not amts:
                if abs(delta) < 0.005:
                    continue  # balance-only row with no movement (B/F lines etc.)
                amount, direction, verified = abs(delta), ("CR" if delta > 0 else "DR"), False
                tags.append("amount_derived")
            else:
                c = _pick(amts)
                amount = c.value
                direction = _hint(c, r) or _guess(narr)
                verified = False
                expected = round(prev + (amount if direction == "CR" else -amount), 2)
        else:
            if not amts:
                if prev is None and balance is not None:
                    prev = balance  # a balance-only first row acts as opening balance
                    inferred_opening = balance
                continue
            c = _pick(amts)
            amount = c.value
            direction = _hint(c, r) or _guess(narr)
            if prev is None and balance is not None:
                inferred_opening = round(balance - (amount if direction == "CR" else -amount), 2)
            if balance is None:
                tags.append("no_balance")
        t = Txn(idx=len(txns) + 1, date=r.date, value_date=r.value_date, narration=narr, amount=round(amount, 2),
                direction=direction, balance=balance, verified=verified, expected_balance=expected,
                page=r.page, tops=r.tops, tags=tags)
        txns.append(t)
        if balance is not None:
            prev = balance
        elif prev is not None:
            prev = round(prev + t.signed, 2)
            t.balance = prev
            t.tags.append("balance_computed")
    if dups:
        warnings.append(f"{len(dups)} row(s) printed twice in the source (same date, narration, amount and "
                        f"balance) - counted once: " + "; ".join(dups[:5]))
    if opening is None:
        opening = inferred_opening
        if opening is not None:
            warnings.append("Opening balance not printed - inferred from the first transaction.")
    return txns, opening, contradictions


def reconciliation(txns: list[Txn], opening: float | None, printed: dict | None,
                   placeholder_opening: bool = False) -> dict:
    n = len(txns)
    ver = sum(1 for t in txns if t.verified is True)
    mis = [t for t in txns if t.verified is False]
    checked = ver + len(mis)
    cr = round(sum(t.credit for t in txns), 2)
    dr = round(sum(t.debit for t in txns), 2)
    closing = next((t.balance for t in reversed(txns) if t.balance is not None and "balance_computed" not in t.tags), None)
    # source printed |balance| after an overdraft and carried on from it: add that jump so closing still reconciles
    src_jump = round(sum(t.balance - t.expected_balance for t in txns
                         if "abs_balance_in_source" in t.tags and t.balance is not None and t.expected_balance is not None), 2)
    computed = round(opening + cr - dr + src_jump, 2) if opening is not None else None
    closing_match = (computed is not None and closing is not None and abs(computed - closing) < 1.0)
    pct = round(100 * ver / checked, 1) if checked else 0.0

    psum_match = None
    if (placeholder_opening and printed and "opening" in printed and abs(printed["opening"]) < 0.005
            and opening is not None and abs(opening) >= 1 and all(k in printed for k in ("credits", "debits", "closing"))
            and abs(abs(abs(printed["credits"]) - abs(printed["debits"])) - abs(printed["closing"])) >= 1):
        # Fino only (pipeline passes placeholder_opening for bank FINO; skeptic 2026-11-20: as a general rule a
        # tamperer could edit just the closing to make the 0.00 'inconsistent'). Fino prints 'Opening Balance 0.00'
        # while its own credits - debits != closing (rows open at 165,407.63): the field is a placeholder, not a
        # figure - compare the rest of the summary (debits, credits, closing, counts) without it
        printed = {k: v for k, v in printed.items() if k != "opening"}
        printed["opening_placeholder"] = 0.0
    if printed:
        n_dr = sum(1 for t in txns if t.direction == "DR")
        bases = [(dr, cr, n_dr, n - n_dr)]
        # contra entries printed as negative numbers in the opposite column: banks that print them that way
        # total each column signed (Debits = withdrawals - reversals, Dr Count includes the reversals)
        neg_d = [t.amount for t in txns if "negative_in_debit_col" in t.tags]
        neg_c = [t.amount for t in txns if "negative_in_credit_col" in t.tags]
        if neg_d or neg_c:
            sd, sc = sum(neg_d), sum(neg_c)
            bases.append((round(dr - sc - sd, 2), round(cr - sd - sc, 2),
                          n_dr - len(neg_c) + len(neg_d), n - n_dr - len(neg_d) + len(neg_c)))
        results = []
        for b_dr, b_cr, b_ndr, b_ncr in bases:
            checks = []
            if "debits" in printed:
                checks.append(abs(abs(printed["debits"]) - b_dr) < 1.0)
            if "credits" in printed:
                checks.append(abs(abs(printed["credits"]) - b_cr) < 1.0)
            if "closing" in printed and closing is not None:
                checks.append(abs(abs(printed["closing"]) - abs(closing)) < 1.0)
            if "opening" in printed and opening is not None:
                checks.append(abs(abs(printed["opening"]) - abs(opening)) < 1.0)
            if "dr_count" in printed:
                checks.append(int(printed["dr_count"]) == b_ndr)
            if "cr_count" in printed:
                checks.append(int(printed["cr_count"]) == b_ncr)
            if checks:
                results.append(all(checks))
        psum_match = any(results) if results else None

    if checked == 0:
        status = "UNVERIFIED"
    elif pct >= 99.5 and closing_match and psum_match is not False:
        status = "MATCHED"
    elif pct >= 85:
        status = "PARTIAL"
    else:
        status = "MISMATCH"
    return {
        "status": status, "opening_balance": opening, "closing_balance": closing,
        "total_credits": cr, "total_debits": dr, "computed_closing": computed, "closing_match": bool(closing_match),
        "rows_total": n, "rows_verified": ver, "rows_mismatch": len(mis), "verified_pct": pct,
        "mismatch_rows": [{"idx": t.idx, "date": t.date.isoformat(), "narration": t.narration[:120],
                           "amount": t.signed, "balance": t.balance, "expected_balance": t.expected_balance}
                          for t in mis[:60]],
        "printed_summary": printed, "printed_summary_match": psum_match,
    }
