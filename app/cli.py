"""Manage client accounts. There is no public sign-up: you create every client here.

    python -m app.cli add EMAIL "Name"     # asks for a password
    python -m app.cli password EMAIL       # set a new password, logs them out everywhere
    python -m app.cli disable EMAIL        # block login, logs them out everywhere
    python -m app.cli enable EMAIL
    python -m app.cli list
    python -m app.cli credit EMAIL 1000 "UPI UTR 412345678901"   # add Rs. 1000 after the client pays
    python -m app.cli balance EMAIL
"""
import argparse
import getpass
import sys
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select

from . import wallet
from .db import Base, SessionLocal, engine
from .models import Client, LoginSession
from .security import MIN_PASSWORD, hash_password


def _ask_password() -> str:
    pw = getpass.getpass("Password: ")
    if len(pw) < MIN_PASSWORD:
        sys.exit(f"Password must be at least {MIN_PASSWORD} characters.")
    if pw != getpass.getpass("Again: "):
        sys.exit("Passwords don't match.")
    return pw


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("email")
    add.add_argument("name")
    for cmd in ("password", "disable", "enable", "balance"):
        sub.add_parser(cmd).add_argument("email")
    cr = sub.add_parser("credit")
    cr.add_argument("email")
    cr.add_argument("rupees")
    cr.add_argument("note", help='Payment reference, e.g. "UPI UTR 412345678901"')
    sub.add_parser("list")
    args = p.parse_args()

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if args.cmd == "list":
            for c in db.scalars(select(Client).order_by(Client.id)):
                print(f"{c.id:>4}  {'active  ' if c.is_active else 'disabled'}  {c.email}  ({c.name})")
            return

        email = args.email.strip().lower()
        client = db.scalar(select(Client).where(Client.email == email))

        if args.cmd == "add":
            if client:
                sys.exit(f"{email} already exists.")
            db.add(Client(email=email, name=args.name.strip(), password_hash=hash_password(_ask_password())))
        elif not client:
            sys.exit(f"No client with email {email}.")
        elif args.cmd == "password":
            client.password_hash = hash_password(_ask_password())
            db.execute(delete(LoginSession).where(LoginSession.client_id == client.id))
        elif args.cmd == "disable":
            client.is_active = False
            db.execute(delete(LoginSession).where(LoginSession.client_id == client.id))
        elif args.cmd == "enable":
            client.is_active = True
        elif args.cmd == "balance":
            print(f"Rs. {wallet.balance(db, client.id) / 100:,.2f}")
            return
        elif args.cmd == "credit":
            try:
                paise = int((Decimal(args.rupees) * 100).to_integral_exact())
            except (InvalidOperation, ArithmeticError):
                sys.exit("Amount must be in rupees, e.g. 1000 or 499.50")
            if paise <= 0:
                sys.exit("Amount must be more than 0.")
            new = wallet.credit(db, client.id, paise, "recharge", args.note.strip()[:200])
            print(f"Credited Rs. {paise / 100:,.2f}. New balance Rs. {new / 100:,.2f}.")
        db.commit()
        print("Done.")


if __name__ == "__main__":
    main()
