"""Configuration stays bootable and its hand-written guide stays complete."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from backend.app import config
from backend.app.config import ROOT, Settings, _settings_with_invalid_values_defaulted


def test_one_invalid_value_defaults_without_discarding_valid_settings(monkeypatch, capsys):
    monkeypatch.setenv("PREVIEW_EVERY", "not-a-number")
    monkeypatch.setenv("LOCAL_OFFLOAD", "not-a-boolean")
    monkeypatch.setenv("PORT", "8765")

    loaded = _settings_with_invalid_values_defaulted()
    assert loaded.preview_every == Settings.model_fields["preview_every"].default
    assert loaded.local_offload == Settings.model_fields["local_offload"].default
    assert loaded.port == 8765
    warning = capsys.readouterr().err
    assert "PREVIEW_EVERY='not-a-number' is invalid" in warning
    assert "LOCAL_OFFLOAD='not-a-boolean' is invalid" in warning


def test_env_example_documents_every_public_setting():
    text = (Path(ROOT) / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))
    declared = {
        str(field.alias or name)
        for name, field in Settings.model_fields.items()
    }
    assert declared - documented == set()
    assert documented - declared == set()


def test_corrupt_runtime_settings_are_preserved_before_replacement(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_OVERRIDE_WARNING", "")
    monkeypatch.setattr(config, "_OVERRIDE_SAVE_BLOCKED", False)
    candidate = Settings(OUTPUT_DIR=str(tmp_path / "output"))
    candidate.ensure_dirs()
    path = candidate.runtime_overrides_path
    broken = b'{"remote_gpu_base_url": "unterminated"'
    path.write_bytes(broken)

    assert candidate.load_overrides() == {}
    backups = list(path.parent.glob("runtime_settings.corrupt-*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == broken
    assert not path.exists()
    assert backups[0].name in candidate.override_warning

    candidate.save_overrides({"embed_metadata": False})
    assert candidate.load_overrides() == {"embed_metadata": False}
    assert backups[0].read_bytes() == broken
    if os.name == "posix":
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_valid_legacy_runtime_settings_are_repaired_to_owner_only(tmp_path):
    candidate = Settings(OUTPUT_DIR=str(tmp_path / "output"))
    candidate.ensure_dirs()
    path = candidate.runtime_overrides_path
    path.write_text('{"remote_gpu_shared_secret": "private"}', encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o644)

    assert candidate.load_overrides() == {"remote_gpu_shared_secret": "private"}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
