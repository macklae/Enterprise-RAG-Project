"""Local demo authentication utilities."""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Generate a secure Argon2 password hash."""
    return _password_hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password without exposing the stored hash."""
    try:
        return _password_hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False
# ---------------------------------------------------------------- sessions

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

AUTH_DB = Path(__file__).resolve().parent.parent / "data" / "auth" / "users.sqlite3"
SESSION_LIFETIME = 60 * 60  # 60 minutes


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate_user(username: str, password: str):
    """Verify credentials and return the account's server-assigned identity."""
    with sqlite3.connect(AUTH_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT id, username, password_hash, role, manager_name
               FROM users WHERE username = ? AND active = 1""",
            (username,),
        ).fetchone()

    if row is None or not verify_password(password, row["password_hash"]):
        return None

    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "manager_name": row["manager_name"],
    }


def create_session(user_id: int):
    """Create an expiring session, storing only the token hash."""
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = int(time.time())

    with sqlite3.connect(AUTH_DB) as conn:
        conn.execute(
            """INSERT INTO sessions
               (token_hash, user_id, csrf_token, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (_token_hash(token), user_id, csrf_token,
             now + SESSION_LIFETIME, now),
        )

    return {"token": token, "csrf_token": csrf_token}


def get_session(token: str):
    """Resolve an active session to its database-assigned user and role."""
    if not token:
        return None

    with sqlite3.connect(AUTH_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT u.id, u.username, u.role, u.manager_name,
                      s.csrf_token, s.expires_at
               FROM sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.token_hash = ? AND u.active = 1""",
            (_token_hash(token),),
        ).fetchone()

    if row is None or row["expires_at"] <= int(time.time()):
        return None

    return dict(row)


def revoke_session(token: str):
    """Invalidate a session during logout."""
    if not token:
        return

    with sqlite3.connect(AUTH_DB) as conn:
        conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?",
            (_token_hash(token),),
        )
