"""Manage client accounts. There is no public sign-up: you create every client here.

    python -m app.cli add EMAIL "Name"     # asks for a password
    python -m app.cli password EMAIL       # set a new password, logs them out everywhere
    python -m app.cli disable EMAIL        # block login, logs them out everywhere
    python -m app.cli enable EMAIL
    python -m app.cli list
"""
import argparse
import getpass
import sys

from sqlalchemy import delete, select

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
    for cmd in ("password", "disable", "enable"):
        sub.add_parser(cmd).add_argument("email")
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
        db.commit()
        print("Done.")


if __name__ == "__main__":
    main()
