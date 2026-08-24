"""Pairing and bearer-session authentication for the local FastAPI surface."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import AuthSession, PairingChallenge, new_app_id
from .repository import ApplicationRepository


class AuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session: AuthSession
    token: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _code_digest(salt: str, code: str) -> str:
    return _digest(f"{salt}:{code}")


class AuthManager:
    def __init__(
        self,
        repository: ApplicationRepository,
        *,
        challenge_ttl: timedelta = timedelta(minutes=5),
        session_ttl: timedelta = timedelta(days=30),
        max_attempts: int = 10,
    ) -> None:
        if challenge_ttl.total_seconds() <= 0 or session_ttl.total_seconds() <= 0:
            raise ValueError("authentication TTLs must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.repository = repository
        self.challenge_ttl = challenge_ttl
        self.session_ttl = session_ttl
        self.max_attempts = max_attempts

    def create_challenge(self) -> PairingChallenge:
        now = datetime.now(timezone.utc)
        challenge_id = new_app_id("pair")
        code = f"{secrets.randbelow(100_000_000):08d}"
        salt = secrets.token_hex(16)
        expires_at = now + self.challenge_ttl
        with self.repository.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO pairing_challenges(
                    challenge_id, salt, code_digest, attempt_count,
                    created_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, 0, ?, ?, NULL)
                """,
                (
                    challenge_id,
                    salt,
                    _code_digest(salt, code),
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
        return PairingChallenge(
            challenge_id=challenge_id,
            code=code,
            expires_at=expires_at,
        )

    def exchange(
        self,
        challenge_id: str,
        code: str,
        *,
        label: str | None = None,
    ) -> IssuedSession:
        now = datetime.now(timezone.utc)
        failure: str | None = None
        issued: IssuedSession | None = None
        with self.repository.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM pairing_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if row is None:
                failure = "invalid_pairing_challenge"
            elif row["consumed_at"] is not None:
                failure = "pairing_challenge_consumed"
            elif datetime.fromisoformat(row["expires_at"]) <= now:
                failure = "pairing_challenge_expired"
            elif int(row["attempt_count"]) >= self.max_attempts:
                failure = "pairing_challenge_locked"
            else:
                candidate = _code_digest(row["salt"], code)
                if not hmac.compare_digest(candidate, row["code_digest"]):
                    connection.execute(
                        """
                        UPDATE pairing_challenges
                        SET attempt_count = attempt_count + 1
                        WHERE challenge_id = ? AND consumed_at IS NULL
                        """,
                        (challenge_id,),
                    )
                    failure = "invalid_pairing_code"
                else:
                    consumed = connection.execute(
                        """
                        UPDATE pairing_challenges SET consumed_at = ?
                        WHERE challenge_id = ? AND consumed_at IS NULL
                        """,
                        (now.isoformat(), challenge_id),
                    ).rowcount
                    if consumed != 1:
                        failure = "pairing_challenge_raced"
                    else:
                        session_id = new_app_id("session")
                        token = f"cf_session_{secrets.token_urlsafe(32)}"
                        expires_at = now + self.session_ttl
                        connection.execute(
                            """
                            INSERT INTO auth_sessions(
                                session_id, token_digest, label, created_at,
                                expires_at, revoked_at
                            ) VALUES (?, ?, ?, ?, ?, NULL)
                            """,
                            (
                                session_id,
                                _digest(token),
                                label,
                                now.isoformat(),
                                expires_at.isoformat(),
                            ),
                        )
                        issued = IssuedSession(
                            session=AuthSession(
                                session_id=session_id,
                                label=label,
                                created_at=now,
                                expires_at=expires_at,
                            ),
                            token=token,
                        )
        if failure is not None:
            raise AuthenticationError(failure)
        if issued is None:
            raise AuthenticationError("pairing_exchange_failed")
        return issued

    def authenticate(self, token: str) -> AuthSession:
        if not token.startswith("cf_session_") or len(token) < 40:
            raise AuthenticationError("invalid_session_token")
        now = datetime.now(timezone.utc)
        with self.repository.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM auth_sessions WHERE token_digest = ?",
                (_digest(token),),
            ).fetchone()
        if row is None:
            raise AuthenticationError("invalid_session_token")
        if row["revoked_at"] is not None:
            raise AuthenticationError("session_revoked")
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= now:
            raise AuthenticationError("session_expired")
        return AuthSession(
            session_id=row["session_id"],
            label=row["label"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=expires_at,
        )

    def revoke(self, token: str) -> None:
        now = datetime.now(timezone.utc)
        with self.repository.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE token_digest = ? AND revoked_at IS NULL
                """,
                (now.isoformat(), _digest(token)),
            ).rowcount
        if changed != 1:
            raise AuthenticationError("invalid_or_revoked_session")
