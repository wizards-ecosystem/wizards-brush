"""The eager installer and lazy loader share one disk-safety policy."""
from __future__ import annotations

import pytest

from scripts import download_models


def test_model_installer_refuses_before_printing_a_false_success(monkeypatch):
    required = download_models.MIN_MODEL_FETCH_BYTES + download_models.DISK_RESERVE_BYTES
    monkeypatch.setattr(
        download_models,
        "model_download_shortfall",
        lambda model: (required, 2 * 1024 ** 3),
    )
    with pytest.raises(RuntimeError, match=r"Refusing to download acme/huge.*2\.0 GB"):
        download_models._assert_model_space("acme/huge")


def test_model_installer_allows_ready_or_unmeasurable_cache(monkeypatch):
    monkeypatch.setattr(download_models, "model_download_shortfall", lambda model: None)
    download_models._assert_model_space("acme/ready")


def test_setup_does_not_mask_an_essential_model_download_failure():
    text = (download_models.Path(__file__).parents[1] / "scripts" / "setup.sh").read_text()
    line = next(line for line in text.splitlines() if "scripts/download_models.py" in line)
    assert "||" not in line
