"""Strathon admin recovery CLI.

Break-glass operations for an operator with direct server/database access.
This is the recovery path for a self-hosted deployment when the sole owner
is locked out (lost password, TOTP device, and recovery codes) and no SMTP
is configured for the email reset flow.

Because it connects directly to the database (via DATABASE_URL), it does not
require the receiver to be running, and it does not go over HTTP — so it is
only usable by someone who already controls the host and database.

Usage:
    python -m admin_cli reset-password --email owner@example.com
    python -m admin_cli reset-password --email owner@example.com --disable-mfa

`reset-password` sets a freshly generated temporary password and prints it
once. The user must change it on next login. `--disable-mfa` additionally
clears the TOTP secret and recovery codes, for the case where the operator
also lost their second factor.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys


def _gen_temp_password() -> str:
    """Generate a high-entropy temporary password.

    Guarantees at least one letter, digit, and special character so it
    satisfies the password policy (the user is forced to change it on next
    login, but it must still pass validation to be set and used).
    """
    import string
    specials = "!@#$%^&*-_=+"
    chars = [
        secrets.choice(string.ascii_letters),
        secrets.choice(string.digits),
        secrets.choice(specials),
    ]
    pool = string.ascii_letters + string.digits + specials
    chars += [secrets.choice(pool) for _ in range(15)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


async def _reset_password(email: str, disable_mfa: bool) -> int:
    # Imported lazily so `--help` works without a configured DATABASE_URL.
    from database import get_session_maker
    from password import hash_password
    import repositories.users as users_repo
    import repositories.mfa as mfa_repo

    maker = get_session_maker()
    async with maker() as session:
        user = await users_repo.find_by_email(session, email)
        if user is None:
            print(f"error: no user found with email {email!r}", file=sys.stderr)
            return 1

        temp = _gen_temp_password()
        await users_repo.update_password_hash(session, user.id, hash_password(temp))

        # Force a password change on next login so the temporary password is
        # not a lasting credential.
        await users_repo.set_force_password_change(session, user.id, True)

        if disable_mfa:
            await mfa_repo.disable_mfa(session, user.id)

        await session.commit()

    print("Password reset successful.")
    print(f"  User:               {email}")
    print(f"  Temporary password: {temp}")
    if disable_mfa:
        print("  MFA:                disabled (TOTP and recovery codes cleared)")
    print("\nGive this password to the user. They must change it on next login.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="strathon-admin",
        description="Strathon break-glass admin recovery (direct database access).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rp = sub.add_parser(
        "reset-password",
        help="Reset a user's password to a generated temporary value.",
    )
    rp.add_argument("--email", required=True, help="Email of the user to reset.")
    rp.add_argument(
        "--disable-mfa",
        action="store_true",
        help="Also clear the user's TOTP secret and recovery codes.",
    )

    args = parser.parse_args(argv)

    if args.command == "reset-password":
        return asyncio.run(_reset_password(args.email, args.disable_mfa))

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
