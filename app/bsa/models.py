from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Cand:
    """A numeric token found in a row (candidate amount or balance)."""
    value: float
    marker: str | None = None      # 'CR' / 'DR' printed next to the number
    sign: str | None = None        # '+' / '-' prefix
    col: str | None = None         # 'debit' / 'credit' / 'amount' / 'balance' from header geometry
    x0: float = 0.0
    x1: float = 0.0
    has_dec: bool = True


@dataclass
class RawTxn:
    date: date
    value_date: date | None = None
    narration_parts: list[str] = field(default_factory=list)
    cands: list[Cand] = field(default_factory=list)   # all numeric tail tokens, left->right
    dir_hint: str | None = None                       # from a Type column word (CREDIT/DEBIT)
    page: int = 0
    tops: list[float] = field(default_factory=list)   # y positions of lines carrying the numbers (forensics)
    has_balance_line: bool = False
    sign_fixed: bool = False                          # balance printed unsigned, chain proves it is negative


@dataclass
class Txn:
    idx: int
    date: date
    value_date: date | None
    narration: str
    amount: float
    direction: str                 # 'CR' or 'DR'
    balance: float | None
    verified: bool | None          # True = balance delta matches; False = mismatch; None = cannot check
    expected_balance: float | None = None
    page: int = 0
    tops: list[float] = field(default_factory=list)
    category: str = "OTHER"
    counterparty: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def credit(self) -> float:
        return self.amount if self.direction == "CR" else 0.0

    @property
    def debit(self) -> float:
        return self.amount if self.direction == "DR" else 0.0

    @property
    def signed(self) -> float:
        return self.amount if self.direction == "CR" else -self.amount


@dataclass
class ParseResult:
    raw: list[RawTxn]
    opening_balance: float | None = None
    printed_summary: dict | None = None
    header_text: str = ""
    page1_text: str = ""
    pages: int = 0
    text_chars: int = 0
    method: str = ""
    warnings: list[str] = field(default_factory=list)
    table_pages: int = 0  # pages that carry dated rows
    grid_pages: int = 0   # of those, pages whose rows were cut by the bank's own drawn row borders
    aligned_pages: int = 0  # of the rest, pages whose row/narration alignment is proven otherwise (_alignment_proof)
    page_proof: list[dict] = field(default_factory=list)  # per non-grid page: proven / why / gaps
    diag: dict = field(default_factory=dict)  # self-heal evidence: money lines, date lines, header words / x-bands
