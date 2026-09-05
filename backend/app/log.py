"""Central logging setup. LOG_LEVEL (env var or .env, via Settings) controls
verbosity (default INFO).

Logger names reproduce the old `[tag]` print prefixes via the format string, so
console output stays as readable as before.
"""
from __future__ import annotations

import logging

FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def setup() -> None:
    from .config import settings  # late: config itself logs nothing at import

    level = settings.log_level.upper()
    logging.basicConfig(level=level, format=FORMAT, datefmt="%H:%M:%S")
    for n in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(n).setLevel(level)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def debugger(subsystem: str):
    """A debug function for one subsystem, enabled by its own env var.

    Turning on DEBUG globally to diagnose one thing produces so much output that
    people stop doing it. A per-subsystem switch makes it worth using:

        debug = log.debugger("quant")     # WB_DEBUG_QUANT=1
        debug("resolved backend %s", backend)

    When the switch is off the returned callable does nothing and costs one call
    — no level check, no format string evaluation, no logger lookup.

    `WB_DEBUG=1` turns on every subsystem at once, for when you genuinely do want
    all of it.
    """
    import os

    enabled = (os.environ.get(f"WB_DEBUG_{subsystem.upper()}")
               or os.environ.get("WB_DEBUG"))
    if not enabled or enabled.lower() in ("0", "false", "no", ""):
        def off(*_args: object, **_kwargs: object) -> None:
            return None

        return off
    logger = logging.getLogger(subsystem)
    logger.setLevel(logging.DEBUG)
    return logger.debug
