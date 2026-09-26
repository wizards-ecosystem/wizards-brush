"""Where a running session is remembered.

Runtime settings, not `.env`: which pod happens to be up is state, not
configuration, and rewriting a config file on every button press would be wrong.
It lives beside the Remote GPU URL that the UI already writes there.

Persisting it at all is what makes an orphaned rental recoverable. The app can
die in ways that run no shutdown code; on the next start it reads this back and
can show - and stop - what it left running.
"""
from __future__ import annotations

from typing import Any

from ..config import settings
from .base import Session

KEY = "remote_gpu_session"


class SessionStore:
    def load(self) -> Session | None:
        raw: Any = settings.load_overrides().get(KEY)
        if not isinstance(raw, dict) or not raw.get("id"):
            return None
        try:
            return Session.from_dict(raw)
        except (TypeError, ValueError):
            return None     # a corrupt record must not block the app from starting

    def save(self, session: Session) -> None:
        data = settings.load_overrides()
        data[KEY] = session.as_dict()
        settings.save_overrides(data)

    def clear(self) -> None:
        data = settings.load_overrides()
        if data.pop(KEY, None) is not None:
            settings.save_overrides(data)
