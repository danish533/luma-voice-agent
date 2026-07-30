"""Authentication for the ops console.

The console shows customer names, dates, party sizes and partial phone numbers,
and it can mint a LiveKit room token. None of that should be reachable by
anyone who can route to the port.

Secure by default rather than convenient by default: if no password is
configured the server generates one and prints it, the way Jupyter prints a
token. There is no code path that serves the console without a password, so
"we'll add auth later" cannot quietly become "we shipped without auth".
"""

from __future__ import annotations

import os
import secrets

import base64
import hashlib

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "luma_ops"
SESSION_MAX_AGE_S = 8 * 3600  # a shift, not a fortnight

# scrypt from the standard library rather than passlib+bcrypt. It is a proper
# password KDF, it removes two dependencies, and it sidesteps a real
# incompatibility: passlib 1.7.4 probes its bcrypt backend with an over-long
# test value, which bcrypt >= 4.1 rejects outright -- so simply installing
# current versions of both makes every password hash raise.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt_b64), dklen=32, **_SCRYPT
        )
    except (ValueError, TypeError):
        return False
    # Constant time: a byte-by-byte comparison leaks how much of the hash matched.
    return secrets.compare_digest(candidate, base64.b64decode(digest_b64))


class Auth:
    def __init__(self) -> None:
        self.username = os.getenv("OPS_USERNAME", "ops")

        # A pre-hashed password is the production path -- the plaintext then
        # never exists in the environment or a process listing.
        self._hash = os.getenv("OPS_PASSWORD_HASH") or None
        self.generated_password: str | None = None

        if not self._hash:
            password = os.getenv("OPS_PASSWORD")
            if not password:
                password = secrets.token_urlsafe(12)
                self.generated_password = password
            self._hash = hash_password(password)

        # Signing key must outlive a restart, or every session is invalidated
        # on deploy. Generated when absent so a missing secret cannot silently
        # become a *fixed* secret, which would be far worse.
        secret = os.getenv("OPS_SECRET_KEY") or secrets.token_urlsafe(32)
        self._serializer = URLSafeTimedSerializer(secret, salt="luma-ops-session")

    def verify(self, username: str, password: str) -> bool:
        """Both sides are checked so a wrong username costs the same as a wrong
        password -- comparing the username first leaks which one was right."""
        user_ok = secrets.compare_digest(username or "", self.username)
        pass_ok = verify_password(password or "", self._hash)
        return user_ok and pass_ok

    def issue(self, username: str) -> str:
        return self._serializer.dumps({"u": username})

    def session_user(self, request: Request) -> str | None:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=SESSION_MAX_AGE_S)
        except (BadSignature, SignatureExpired):
            return None
        return data.get("u")

    def require(self, request: Request) -> str:
        user = self.session_user(request)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in required")
        return user

    def banner(self) -> list[str]:
        """What to print at startup, so nobody has to go looking for it."""
        if not self.generated_password:
            return [f"  Ops console sign-in: {self.username} (password from the environment)"]
        return [
            "",
            "  No OPS_PASSWORD set, so one was generated for this run:",
            f"      username: {self.username}",
            f"      password: {self.generated_password}",
            "  Set OPS_PASSWORD (or OPS_PASSWORD_HASH) to keep it across restarts.",
            "",
        ]
