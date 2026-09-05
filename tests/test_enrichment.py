"""Enrichment plumbing (no models): keyword extraction and that enqueue never raises."""
from __future__ import annotations

from backend.app import enrichment
from backend.app.config import settings


def test_keywords_filters_stopwords():
    kw = enrichment._keywords("The image shows a red fox jumping over a lazy dog in the snow")
    assert "the" not in kw and "image" not in kw and "shows" not in kw
    assert "red" in kw and "fox" in kw
    assert len(kw) <= 6


def test_enqueue_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "enrich_captions", False)
    enrichment.enqueue(123456)  # feature off → no-op, no worker started
