from pathlib import Path

import pytest

from bitsbots.config import default_database_path


def test_source_checkout_uses_local_data_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr("sys.frozen", raising=False)

    assert default_database_path() == Path("data/bitsbots.db")


def test_packaged_windows_build_uses_local_app_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Test\AppData\Local")

    assert default_database_path() == (
        Path(r"C:\Users\Test\AppData\Local") / "BitsBots" / "bitsbots.db"
    )


def test_packaged_windows_build_requires_local_app_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    with pytest.raises(RuntimeError, match="LOCALAPPDATA"):
        default_database_path()
