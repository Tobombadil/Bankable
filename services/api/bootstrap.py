"""Make the first operator: `python -m services.api.bootstrap owner --email you@example.com`.

Registration through the site creates ordinary members (services/api/auth_routes.py); nobody
can reach `/admin` until a user carries the `owner` role, and until now that meant editing the
database by hand. This command creates the user (asking for a password, or taking `--password`)
or promotes an existing one, gives the account the `admin` entitlement with `entitlement_source
= manual_grant` (the interim override docs/00-PLAN.md keeps on purpose) and enough seats for a
few devices, and prints one line. It targets `DATABASE_URL`, or `--db` for the SQLite file
`web/dev_up.py` writes (`web/.data/dev.db`), so the local prototype and a real deployment use
the same path.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.auth import hash_password
from services.db.models import Account, User
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id

DEFAULT_DEV_DB = Path(__file__).resolve().parents[2] / "web" / ".data" / "dev.db"


def make_owner(
    db: Session, *, email: str, password: str | None, name: str | None, seats: int
) -> tuple[User, bool]:
    """Creates or promotes the user with `email`; returns `(user, created)`."""
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email, User.status != "anonymised"))
    created = user is None
    if user is None:
        if not password:
            raise ValueError("a password is required to create a new owner")
        account = Account(public_id="", name=name or email, kind="organization", entitlement="admin")
        db.add(account)
        db.flush()
        account.public_id = public_id("acc", account.id)
        user = User(
            public_id="",
            account_id=account.id,
            email=email,
            password_hash=hash_password(password),
            name=name,
            role="owner",
            auth_provider="password",
        )
        db.add(user)
        db.flush()
        user.public_id = public_id("usr", user.id)
    else:
        user.role = "owner"
        user.status = "active"
        if password:
            user.password_hash = hash_password(password)
        existing = db.get(Account, user.account_id)
        if existing is None:  # pragma: no cover - a user always has an account
            raise ValueError("user has no account")
        account = existing
    account.entitlement = "admin"
    account.entitlement_source = "manual_grant"
    account.seats = max(account.seats, seats)
    db.flush()
    return user, created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    owner = sub.add_parser("owner", help="create or promote an owner (operator with every admin right)")
    owner.add_argument("--email", required=True)
    owner.add_argument("--password", help="omit to be prompted; ignored when promoting unless given")
    owner.add_argument("--name")
    owner.add_argument("--seats", type=int, default=5)
    owner.add_argument(
        "--db",
        type=Path,
        help=f"SQLite file to target instead of DATABASE_URL (the dev launcher's is {DEFAULT_DEV_DB})",
    )
    args = parser.parse_args(argv)

    url = f"sqlite:///{args.db}" if args.db else None
    engine = get_engine(url)
    init_db(engine)
    password = args.password
    with get_sessionmaker(engine)() as db:
        exists = db.scalar(select(User).where(User.email == args.email.strip().lower())) is not None
        if not exists and not password:
            password = getpass.getpass("Password for the new owner: ")
        user, created = make_owner(db, email=args.email, password=password, name=args.name, seats=args.seats)
        db.commit()
        verb = "created" if created else "promoted"
        sys.stdout.write(
            f"{verb} owner {user.email} ({user.public_id}); sign in at /login, then open /admin\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
