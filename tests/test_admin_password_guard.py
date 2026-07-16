"""Regression tests for S5/#301: ADMIN_PASSWORD must be set in production.

Verifies that _seed_admin_user raises RuntimeError when ADMIN_PASSWORD is empty in
production mode, and falls back to 'changeme' in dev/stub mode.
"""
from unittest.mock import MagicMock, patch


def _run_seed(admin_password: str, use_stub: bool, has_users: bool = False):
    """Call _seed_admin_user with the given settings and return normally or raise."""
    repo_mock = MagicMock()
    repo_mock.sync_list_all.return_value = ["existing"] if has_users else []

    session_cm = MagicMock()
    session_cm.__enter__ = MagicMock(return_value=MagicMock())
    session_cm.__exit__ = MagicMock(return_value=False)

    with (
        patch("app.main.settings") as s,
        patch("app.main.UserRepository", return_value=repo_mock),
        patch("app.main.SyncSessionLocal", return_value=session_cm),
        patch("app.main.bcrypt") as mock_bcrypt,
    ):
        s.ADMIN_PASSWORD = admin_password
        s.USE_STUB_TERRAFORM = use_stub
        s.ADMIN_USERNAME = "admin"
        mock_bcrypt.hashpw.return_value = b"$2b$12$fakehash"
        mock_bcrypt.gensalt.return_value = b"$2b$12$fakesalt"

        from app.main import _seed_admin_user
        _seed_admin_user()

    return repo_mock


def test_production_mode_empty_password_raises():
    """Empty ADMIN_PASSWORD in production (USE_STUB_TERRAFORM=False) must abort startup."""
    import pytest
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD must be set"):
        _run_seed(admin_password="", use_stub=False)


def test_dev_stub_mode_empty_password_uses_changeme():
    """Empty ADMIN_PASSWORD in dev/stub mode seeds with 'changeme' without raising."""
    with (
        patch("app.main.settings") as s,
        patch("app.main.UserRepository") as repo_cls,
        patch("app.main.SyncSessionLocal") as session_factory,
        patch("app.main.bcrypt") as mock_bcrypt,
    ):
        s.ADMIN_PASSWORD = ""
        s.USE_STUB_TERRAFORM = True
        s.ADMIN_USERNAME = "admin"
        repo = MagicMock()
        repo.sync_list_all.return_value = []
        repo_cls.return_value = repo
        session_cm = MagicMock()
        session_cm.__enter__ = MagicMock(return_value=MagicMock())
        session_cm.__exit__ = MagicMock(return_value=False)
        session_factory.return_value = session_cm
        mock_bcrypt.hashpw.return_value = b"$2b$12$fakehash"
        mock_bcrypt.gensalt.return_value = b"$2b$12$fakesalt"

        from app.main import _seed_admin_user
        _seed_admin_user()  # must not raise

    mock_bcrypt.hashpw.assert_called_once()
    call_pw = mock_bcrypt.hashpw.call_args[0][0]
    assert call_pw == b"changeme"


def test_set_password_seeds_correctly():
    """Explicit ADMIN_PASSWORD is used as-is in both modes."""
    with (
        patch("app.main.settings") as s,
        patch("app.main.UserRepository") as repo_cls,
        patch("app.main.SyncSessionLocal") as session_factory,
        patch("app.main.bcrypt") as mock_bcrypt,
    ):
        s.ADMIN_PASSWORD = "supersecret99"
        s.USE_STUB_TERRAFORM = False
        s.ADMIN_USERNAME = "admin"
        repo = MagicMock()
        repo.sync_list_all.return_value = []
        repo_cls.return_value = repo
        session_cm = MagicMock()
        session_cm.__enter__ = MagicMock(return_value=MagicMock())
        session_cm.__exit__ = MagicMock(return_value=False)
        session_factory.return_value = session_cm
        mock_bcrypt.hashpw.return_value = b"$2b$12$fakehash"
        mock_bcrypt.gensalt.return_value = b"$2b$12$fakesalt"

        from app.main import _seed_admin_user
        _seed_admin_user()

    call_pw = mock_bcrypt.hashpw.call_args[0][0]
    assert call_pw == b"supersecret99"


def test_already_seeded_skips_password_check():
    """If users already exist, the password guard is never reached."""
    with (
        patch("app.main.settings") as s,
        patch("app.main.UserRepository") as repo_cls,
        patch("app.main.SyncSessionLocal") as session_factory,
        patch("app.main.bcrypt") as mock_bcrypt,
    ):
        s.ADMIN_PASSWORD = ""
        s.USE_STUB_TERRAFORM = False  # production mode
        s.ADMIN_USERNAME = "admin"
        repo = MagicMock()
        repo.sync_list_all.return_value = ["existing_user"]
        repo_cls.return_value = repo
        session_cm = MagicMock()
        session_cm.__enter__ = MagicMock(return_value=MagicMock())
        session_cm.__exit__ = MagicMock(return_value=False)
        session_factory.return_value = session_cm

        from app.main import _seed_admin_user
        _seed_admin_user()  # must not raise even in prod with empty password

    mock_bcrypt.hashpw.assert_not_called()
