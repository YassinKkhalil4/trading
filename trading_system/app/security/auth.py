from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

try:
    import bcrypt
except ImportError:  # pragma: no cover - dependency is declared for runtime images.
    bcrypt = None  # type: ignore[assignment]

from fastapi import Depends, Header, HTTPException, status

from trading_system.app.core.config import Settings, get_settings
from trading_system.app.core.enums import AdminRole
from trading_system.app.db import models
from trading_system.app.db.repositories import TradingRepository
from trading_system.app.db.session import SessionLocal


PASSWORD_HASH_ALGORITHM = "bcrypt"
LEGACY_PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 390_000
JWT_ALGORITHM = "HS256"
JWT_TYPE = "JWT"


@dataclass(frozen=True)
class AdminPrincipal:
    username: str
    role: str = AdminRole.ADMIN.value
    user_id: str | None = None


@dataclass(frozen=True)
class LoginResult:
    authenticated: bool
    token: str | None
    username: str
    role: str | None
    expires_at: datetime | None
    reason: str


def hash_password(password: str) -> str:
    if bcrypt is None:
        raise RuntimeError("bcrypt package is required for admin password hashing.")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def _hash_password_pbkdf2(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return "$".join(
        [
            LEGACY_PASSWORD_HASH_ALGORITHM,
            str(PASSWORD_HASH_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def verify_password_hash(password: str, stored_hash: str) -> bool:
    if _is_bcrypt_hash(stored_hash):
        if bcrypt is None:
            return False
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("ascii"))
    return _verify_legacy_pbkdf2_password_hash(password, stored_hash)


def password_hash_needs_upgrade(stored_hash: str) -> bool:
    return not _is_bcrypt_hash(stored_hash)


def _is_bcrypt_hash(stored_hash: str) -> bool:
    return stored_hash.startswith(("$2a$", "$2b$", "$2y$"))


def _verify_legacy_pbkdf2_password_hash(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = stored_hash.split("$", 3)
        if algorithm != LEGACY_PASSWORD_HASH_ALGORITHM:
            return False
        iterations = int(iterations_raw)
        salt = base64.urlsafe_b64decode(salt_raw.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_raw.encode("ascii"))
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_session_token(
    *,
    user_id: str,
    username: str,
    role: str,
    expires_at: datetime,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": secrets.token_urlsafe(18),
    }
    header = {"alg": JWT_ALGORITHM, "typ": JWT_TYPE}
    signing_input = ".".join([_b64url_json(header), _b64url_json(payload)])
    signature = _sign_jwt(signing_input, settings)
    return f"{signing_input}.{signature}"


def decode_session_token(token: str, settings: Settings | None = None) -> dict[str, Any] | None:
    settings = settings or get_settings()
    try:
        header_raw, payload_raw, signature = token.split(".", 2)
    except ValueError:
        return None
    signing_input = f"{header_raw}.{payload_raw}"
    if not hmac.compare_digest(signature, _sign_jwt(signing_input, settings)):
        return None
    header = _b64url_decode_json(header_raw)
    payload = _b64url_decode_json(payload_raw)
    if header.get("alg") != JWT_ALGORITHM or header.get("typ") != JWT_TYPE:
        return None
    exp = int(payload.get("exp") or 0)
    if exp <= int(datetime.now(UTC).timestamp()):
        return None
    return payload


def hash_session_token(token: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    secret = settings.admin_session_secret or "change-me"
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_admin_password(username: str, password: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not settings.admin_password:
        return False
    return hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
        password, settings.admin_password
    )


class AuthService:
    def __init__(self, repository: TradingRepository, settings: Settings | None = None) -> None:
        self.repository = repository
        self.settings = settings or get_settings()

    def bootstrap_configured_admin(self) -> models.AdminUser | None:
        if not self.settings.admin_password:
            return None
        existing = self.repository.admin_user_by_username(self.settings.admin_username)
        if existing:
            return existing
        return self.repository.upsert_admin_user(
            username=self.settings.admin_username,
            password_hash=hash_password(self.settings.admin_password),
            role=AdminRole.ADMIN.value,
            reason="Bootstrapped from configured ADMIN_USERNAME/ADMIN_PASSWORD.",
        )

    def bootstrap_admin(self) -> models.AdminUser | None:
        return self.bootstrap_configured_admin()

    def login(self, username: str, password: str) -> LoginResult:
        self.bootstrap_configured_admin()
        user = self.repository.admin_user_by_username(username)
        now = datetime.now(UTC)
        if not user or not user.is_active:
            self.repository.store_audit_log(
                actor=username,
                event_type="FAILED_LOGIN",
                entity_type="admin_user",
                entity_id=None,
                reason="Unknown or inactive admin user.",
                payload=None,
            )
            return LoginResult(False, None, username, None, None, "Invalid credentials.")
        locked_until = _as_utc(user.locked_until)
        if locked_until and locked_until > now:
            reason = f"User is locked until {locked_until.isoformat()}."
            self.repository.store_audit_log(
                actor=username,
                event_type="FAILED_LOGIN_LOCKED",
                entity_type="admin_user",
                entity_id=user.id,
                reason=reason,
                payload=None,
            )
            return LoginResult(False, None, username, user.role, None, reason)
        if not verify_password_hash(password, user.password_hash):
            next_count = user.failed_login_count + 1
            lock_until = None
            if next_count >= self.settings.admin_failed_login_lockout_attempts:
                lock_until = now + timedelta(minutes=self.settings.admin_lockout_minutes)
            self.repository.record_failed_login(username, locked_until=lock_until)
            self.repository.store_audit_log(
                actor=username,
                event_type="FAILED_LOGIN",
                entity_type="admin_user",
                entity_id=user.id,
                reason="Invalid credentials.",
                payload={"failed_login_count": next_count, "locked_until": lock_until.isoformat() if lock_until else None},
            )
            return LoginResult(False, None, username, user.role, None, "Invalid credentials.")

        expires_at = now + timedelta(minutes=self.settings.auth_session_minutes)
        token = create_session_token(
            user_id=user.id,
            username=user.username,
            role=user.role,
            expires_at=expires_at,
            settings=self.settings,
        )
        if password_hash_needs_upgrade(user.password_hash):
            user.password_hash = hash_password(password)
            user.reason = "Password hash upgraded to bcrypt after successful authentication."
        self.repository.record_successful_login(user)
        self.repository.store_admin_session(
            user_id=user.id,
            token_hash=hash_session_token(token, self.settings),
            expires_at=expires_at,
            reason="Admin session created after successful login.",
        )
        self.repository.store_audit_log(
            actor=username,
            event_type="LOGIN",
            entity_type="admin_user",
            entity_id=user.id,
            reason="Successful admin login.",
            payload={"role": user.role, "expires_at": expires_at.isoformat()},
        )
        return LoginResult(True, token, username, user.role, expires_at, "Login successful.")

    def authenticate_token(self, token: str) -> AdminPrincipal | None:
        jwt_payload = decode_session_token(token, self.settings)
        session = self.repository.admin_session_by_hash(hash_session_token(token, self.settings))
        if not session:
            return None
        user = self.repository.admin_user_by_id(session.user_id)
        if not user or not user.is_active:
            return None
        if jwt_payload and jwt_payload.get("sub") != user.id:
            return None
        return AdminPrincipal(username=user.username, role=user.role, user_id=user.id)

    def logout(self, token: str, actor: str | None = None) -> bool:
        token_hash = hash_session_token(token, self.settings)
        revoked = self.repository.revoke_admin_session(
            token_hash,
            reason="Admin session revoked by logout.",
        )
        self.repository.store_audit_log(
            actor=actor or "unknown",
            event_type="LOGOUT",
            entity_type="admin_session",
            entity_id=None,
            reason="Admin logout requested.",
            payload={"revoked": revoked},
        )
        return revoked


def require_principal(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> AdminPrincipal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    token = authorization.removeprefix("Bearer ").strip()
    if settings.api_admin_token and hmac.compare_digest(token, settings.api_admin_token):
        return AdminPrincipal(username=settings.admin_username, role=AdminRole.ADMIN.value)

    session = SessionLocal()
    try:
        repository = TradingRepository(session)
        principal = AuthService(repository, settings).authenticate_token(token)
        if not principal:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session.")
        return principal
    finally:
        session.close()


def require_admin_token(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> AdminPrincipal:
    principal = require_principal(authorization, settings)
    if principal.role != AdminRole.ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required.")
    return principal


def require_trader_or_admin(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> AdminPrincipal:
    principal = require_principal(authorization, settings)
    if principal.role not in {AdminRole.ADMIN.value, AdminRole.TRADER.value}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Trader or admin role required.")
    return principal


def _b64url_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _b64url_decode_json(payload: str) -> dict[str, Any]:
    padded = payload + "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def _sign_jwt(signing_input: str, settings: Settings) -> str:
    secret = settings.admin_session_secret or "change-me"
    digest = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
