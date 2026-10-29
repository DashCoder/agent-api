"""Small helpers:  python -m app.cli hash-password"""
import getpass
import sys

from .auth import hash_password


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "hash-password":
        print("usage: python -m app.cli hash-password")
        raise SystemExit(1)
    pw = getpass.getpass("Admin password: ")
    if pw != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords don't match")
    print(f"ADMIN_PASSWORD_HASH={hash_password(pw)}")


if __name__ == "__main__":
    main()
