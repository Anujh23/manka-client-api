"""Price list. One line per product API, in paise per successful call (₹1 = 100).

Live keys pay from the client's wallet. Test keys pay the same price from a one-time test credit
(TEST_CREDIT_PAISE per client), so testing is capped. A product route charges with
`Depends(charged("<name>"))` from api.py; an unknown name fails at startup, so every route needs a price.
"""

TEST_CREDIT_PAISE = 100_00  # ₹100 of test calls per client

PRICES: dict[str, int] = {
    "ping": 0,  # key check, always free
}
