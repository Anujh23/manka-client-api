"""Parse options a self-heal strategy can override (see selfheal.py). The default Opts() changes nothing."""
from __future__ import annotations

import time

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Grammar:
    """A date grammar learned from the document's own tokens: regex with 3 groups in reading order,
    order = the role of each group ('d','m','y'), month_name = the month group is a month word."""
    shape: str
    pattern: str
    order: tuple
    month_name: bool
    year2: bool = False

    def compiled(self):
        import re
        return re.compile(self.pattern, re.I)


@dataclass(frozen=True)
class Opts:
    name: str = "default"
    grammars: tuple = ()          # extra date grammars (added after the built-in patterns)
    force_mode: str | None = None  # None | 'below' | 'centered'
    row_cut: str = "date"          # 'date' = rows start at date lines | 'amount_end' = rows end at amount+balance lines
    order: str | None = None       # None = resolve picks | printed | reversed | sort_printed | sort_reversed (only that one)
    col_roles: str = "header"      # 'header' = money columns from the header words | 'xcluster' = from numeric x-clusters
    # not a strategy parameter: shared page-word cache so the portfolio extracts text once
    cache: dict | None = field(default=None, compare=False, hash=False)


OPTS: ContextVar[Opts] = ContextVar("bsa_opts", default=Opts())


@contextmanager
def use(opts: Opts | None):
    tok = OPTS.set(opts or Opts())
    try:
        yield
    finally:
        OPTS.reset(tok)


class DeadlineExceeded(BaseException):
    """Raised by check_deadline() inside a self-heal strategy that ran out of its time budget. A BaseException so
    no generic except-Exception in the parser can swallow it."""


# monotonic deadline of the strategy now running (None = the normal parse: no deadline, nothing ever raises)
DEADLINE: ContextVar[float | None] = ContextVar("bsa_deadline", default=None)


def check_deadline() -> None:
    d = DEADLINE.get()
    if d is not None and time.monotonic() > d:
        raise DeadlineExceeded()


@contextmanager
def deadline(t: float | None):
    tok = DEADLINE.set(t)
    try:
        yield
    finally:
        DEADLINE.reset(tok)
