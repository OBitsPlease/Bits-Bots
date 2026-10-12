from dataclasses import replace
from pathlib import Path

import pytest

from bitsbots.auth import AuthenticationError, AuthService
from bitsbots.config import SETTINGS
from bitsbots.storage import Storage


@pytest.fixture
def auth(tmp_path: Path) -> tuple[AuthService, Storage]:
    settings = replace(SETTINGS, database_path=tmp_path / "auth.db")
    storage = Storage(settings.database_path)
    yield AuthService(storage, settings), storage
    storage.close()


def test_registration_hashes_password_and_creates_session(
    auth: tuple[AuthService, Storage],
) -> None:
    service, storage = auth
    session = service.register(
        "Trader@example.com", "Local Trader", "correct horse battery"
    )
    stored = storage.user_with_password("trader@example.com")
    assert stored is not None
    assert stored[1] != "correct horse battery"
    assert service.authenticate(session.token) == session.user
    assert storage.cash(session.user.id) == SETTINGS.starting_cash


def test_logout_invalidates_session(auth: tuple[AuthService, Storage]) -> None:
    service, _ = auth
    session = service.register(
        "trader@example.com", "Local Trader", "correct horse battery"
    )
    service.logout(session.token)
    assert service.authenticate(session.token) is None


def test_verify_password_reconfirms_only_the_correct_password(
    auth: tuple[AuthService, Storage],
) -> None:
    service, _ = auth
    session = service.register(
        "trader@example.com", "Local Trader", "correct horse battery"
    )
    assert service.verify_password(session.user, "correct horse battery")
    assert not service.verify_password(session.user, "wrong password")


def test_login_rejects_wrong_password(auth: tuple[AuthService, Storage]) -> None:
    service, _ = auth
    service.register("trader@example.com", "Local Trader", "correct horse battery")
    with pytest.raises(AuthenticationError, match="Invalid email or password"):
        service.login("trader@example.com", "wrong password")


def test_user_portfolios_are_isolated(auth: tuple[AuthService, Storage]) -> None:
    service, storage = auth
    first = service.register(
        "first@example.com", "First Trader", "correct horse battery"
    )
    second = service.register(
        "second@example.com", "Second Trader", "another secure password"
    )
    storage.set_cash(first.user.id, 7500)
    assert storage.cash(first.user.id) == 7500
    assert storage.cash(second.user.id) == SETTINGS.starting_cash


def test_user_bots_are_isolated(auth: tuple[AuthService, Storage]) -> None:
    service, storage = auth
    first = service.register(
        "first@example.com", "First Trader", "correct horse battery"
    )
    second = service.register(
        "second@example.com", "Second Trader", "another secure password"
    )
    bot = storage.create_bot(
        first.user.id, "BTC Scout", "BTC/USDT", "Multi-TF", "medium", 70, 0.1
    )
    assert storage.list_bots(first.user.id) == [bot]
    assert storage.list_bots(second.user.id) == []
    assert storage.update_bot_status(second.user.id, bot.id, "paused") is None
    assert storage.list_bots(first.user.id)[0].status == "running"
