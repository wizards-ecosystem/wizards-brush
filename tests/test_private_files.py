"""Owner-only credential files: 0600 on POSIX, an owner-only ACL on Windows."""
from __future__ import annotations

import os
import stat

import pytest

from backend.app.utils import private_files

_PATH = r"C:\Users\Ada Lovelace\wb\.env"


def test_an_owner_only_listing_is_private():
    listing = (f"{_PATH} DESKTOP-1\\ada:(F)\n\n"
               "Successfully processed 1 files; Failed processing 0 files\n")
    assert private_files.grants_only(listing, _PATH, "DESKTOP-1\\ada")


def test_an_inherited_users_grant_is_not_private():
    listing = (f"{_PATH} DESKTOP-1\\ada:(I)(F)\n"
               "          BUILTIN\\Users:(I)(RX)\n"
               "          NT AUTHORITY\\SYSTEM:(I)(F)\n\n"
               "Successfully processed 1 files; Failed processing 0 files\n")
    assert not private_files.grants_only(listing, _PATH, "DESKTOP-1\\ada")


def test_an_empty_listing_proves_nothing():
    assert not private_files.grants_only("", _PATH, "DESKTOP-1\\ada")


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_posix_restrict_sets_0600(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("API_TOKEN=x\n")
    os.chmod(secret, 0o644)
    assert private_files.is_private(secret) is False
    private_files.restrict(secret)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert private_files.is_private(secret) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows access lists")
def test_windows_restrict_leaves_only_this_account(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("API_TOKEN=x\n")
    private_files.restrict(secret)
    assert private_files.is_private(secret) is True
