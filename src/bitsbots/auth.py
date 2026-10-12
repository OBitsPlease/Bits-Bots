import hashlib
import secrets
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .config import Settings
from .models import User
from .storage import Storage


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class Session:
    token: str
    expires_at: int
    user: User


class AuthService:
    def __init__(self, storage: Storage, settings: Settings) -> None:
        self.storage = storage
        self.settings = settings
        self.password_hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
        )

    def register(self, email: str, display_name: str, password: str) -> Session:
        self._validate_profile(email, display_name, password)
        user = self.storage.create_user(
            email, display_name, self.password_hasher.hash(password)
        )
        self.storage.initialize_account(user.id, self.settings.starting_cash)
        return self._create_session(user)

    def login(self, email: str, password: str) -> Session:
        result = self.storage.user_with_password(email)
        if result is None:
            self.password_hasher.hash(password)
            raise AuthenticationError("Invalid email or password")
        user, password_hash = result
        try:
            valid = self.password_hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError) as exc:
            raise AuthenticationError("Invalid email or password") from exc
        if not valid:
            raise AuthenticationError("Invalid email or password")
        if self.password_hasher.check_needs_rehash(password_hash):
            self.storage.update_password_hash(
                user.id, self.password_hasher.hash(password)
            )
        return self._create_session(user)

    def authenticate(self, token: str | None) -> User | None:
        if not token:
            return None
        return self.storage.user_for_session(
            self._hash_token(token), int(time.time() * 1000)
        )

    def logout(self, token: str | None) -> None:
        if token:
            self.storage.delete_session(self._hash_token(token))

    def verify_password(self, user: User, password: str) -> bool:
        result = self.storage.user_with_password(user.email)
        if result is None or result[0].id != user.id:
            return False
        try:
            return bool(self.password_hasher.verify(result[1], password))
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def _create_session(self, user: User) -> Session:
        token = secrets.token_urlsafe(48)
        now = int(time.time() * 1000)
        expires_at = now + self.settings.session_lifetime_days * 86_400_000
        self.storage.save_session(self._hash_token(token), user.id, now, expires_at)
        return Session(token=token, expires_at=expires_at, user=user)

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_profile(email: str, display_name: str, password: str) -> None:
        normalized_email = email.strip()
        if (
            len(normalized_email) > 254
            or "@" not in normalized_email
            or normalized_email.startswith("@")
            or normalized_email.endswith("@")
        ):
            raise AuthenticationError("Enter a valid email address")
        if not 2 <= len(display_name.strip()) <= 80:
            raise AuthenticationError("Display name must be 2 to 80 characters")
        if len(password) < 12:
            raise AuthenticationError("Password must be at least 12 characters")
        if len(password) > 1024:
            raise AuthenticationError("Password is too long")
