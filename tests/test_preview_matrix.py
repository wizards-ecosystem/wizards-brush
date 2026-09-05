"""Fitted latent-to-RGB projections, and the fallback when we have none."""
from __future__ import annotations

from backend.app.generators import latent_rgb


def test_no_entry_means_no_matrix():
    assert latent_rgb.matrix_for("some/model-we-never-fitted") is None
    assert latent_rgb.matrix_for(None) is None
    assert latent_rgb.matrix_for("") is None


def test_a_fitted_entry_is_found(monkeypatch):
    m = ([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], [0.5, 0.5, 0.5])
    monkeypatch.setattr(latent_rgb, "MATRICES", {"Org/Model-Turbo": m})
    assert latent_rgb.matrix_for("Org/Model-Turbo") == m


def test_lookup_falls_back_to_the_repo_stem(monkeypatch):
    """A local path or a revision-pinned id still finds the canonical fit."""
    m = ([[1.0, 0.0, 0.0]], None)
    monkeypatch.setattr(latent_rgb, "MATRICES", {"Org/Model-Turbo": m})
    assert latent_rgb.matrix_for("/home/me/models/Model-Turbo") == m
    assert latent_rgb.matrix_for("Fork/Model-Turbo") == m


def test_an_unrelated_stem_does_not_match(monkeypatch):
    monkeypatch.setattr(latent_rgb, "MATRICES", {"Org/Model-Turbo": ([[1.0, 0, 0]], None)})
    assert latent_rgb.matrix_for("Org/Different-Model") is None


def test_the_shipped_table_is_well_formed():
    """Every entry must be [channels][3] with a matching bias, or the projection
    silently produces garbage at generation time rather than failing here."""
    for model, (matrix, bias) in latent_rgb.MATRICES.items():
        assert matrix, f"{model}: empty matrix"
        assert all(len(row) == 3 for row in matrix), f"{model}: rows must be RGB triples"
        assert bias is None or len(bias) == 3, f"{model}: bias must be an RGB triple"
