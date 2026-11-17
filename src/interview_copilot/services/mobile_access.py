"""In-memory, single-use pairing and revocable grants for one active session."""

from collections import deque
from dataclasses import dataclass
import hashlib
import secrets
import threading
import time


class PairingError(ValueError):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Grant:
    session_id: str
    expires_at: float


class MobileAccess:
    def __init__(self, *, clock=time.monotonic, code_ttl=120, grant_ttl=8 * 3600):
        self.clock, self.code_ttl, self.grant_ttl = clock, code_ttl, grant_ttl
        self._lock = threading.RLock()
        self._code = None
        self._session = None
        self._deadline = 0
        self._grants = {}
        self._attempts = deque()

    def _prune(self):
        now = self.clock()
        self._grants = {key: grant for key, grant in self._grants.items() if grant.expires_at > now}
        while self._attempts and self._attempts[0][0] <= now - 60:
            self._attempts.popleft()

    def issue(self, session_id):
        with self._lock:
            self._code = f"{secrets.randbelow(100000000):08d}"
            self._session, self._deadline = session_id, self.clock() + self.code_ttl
            return self.challenge(session_id)

    def challenge(self, session_id):
        with self._lock:
            valid = self._session == session_id and self._deadline > self.clock()
            return {"code": self._code if valid else None,
                    "expires_in": max(0, int(self._deadline - self.clock())) if valid else 0}

    def exchange(self, code, session_id, peer):
        with self._lock:
            self._prune()
            if len(self._attempts) >= 30 or sum(ip == peer for _, ip in self._attempts) >= 5:
                raise PairingError("Too many attempts. Try again in one minute.", 429)
            self._attempts.append((self.clock(), peer))
            if (not isinstance(code, str) or len(code) != 8 or self._code is None
                    or self._session != session_id or self.clock() >= self._deadline
                    or not secrets.compare_digest(code, self._code)):
                raise PairingError("Pairing code is invalid or expired.")
            if len(self._grants) >= 8:
                raise PairingError("Device limit reached. Revoke access on the desktop.", 409)
            token = secrets.token_urlsafe(32)
            self._grants[self._digest(token)] = Grant(session_id, self.clock() + self.grant_ttl)
            self._code = None
            return token

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def authorize(self, token, session_id):
        with self._lock:
            self._prune()
            grant = self._grants.get(self._digest(token)) if isinstance(token, str) and len(token) <= 128 else None
            return bool(grant and grant.session_id == session_id)

    def revoke(self, token):
        with self._lock:
            if isinstance(token, str):
                self._grants.pop(self._digest(token), None)

    def revoke_all(self):
        with self._lock:
            self._grants.clear()
            self._code = None
            self._session = None

    def count(self, session_id):
        with self._lock:
            self._prune()
            return sum(grant.session_id == session_id for grant in self._grants.values())
