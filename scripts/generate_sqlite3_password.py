#!/usr/bin/env python3
import secrets
import string

def generate_sqlite_password(length: int = 32) -> str:
    """
    Generate a strong password suitable for use as a SQLCipher/SQLite encryption passphrase.
    Uses a URL/file/env-friendly character set.
    """
    alphabet = string.ascii_letters + string.digits + "-_!@#$%^&*"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in "-_!@#$%^&*" for c in password)
        ):
            return password

if __name__ == "__main__":
    password = generate_sqlite_password(32)
    print(password)
