"""Job.model_key plus the composite index the lane's dequeue actually uses.

`model_key` records which local model a job needs, resolved once when the job is
created. The scheduler compares it against the resident model to avoid a 60-120
second pipeline swap; doing that by decoding `params_json` at dequeue time would
put a JSON parse on the hot path for every candidate it considers.

NULL means "no local model involved" — remote jobs, and tool jobs like upscale
that never touch the diffusion pipeline.

The composite index matches the dequeue query shape exactly:
`WHERE status = 'queued' ORDER BY priority DESC, id ASC`. Without it SQLite can
use at most one of the single-column indexes and sorts the remainder, and that
cost grows with total job history rather than with the number of queued jobs.
"""
from __future__ import annotations

import json
import sqlite3

from .ops import add_column, add_index, table_exists

# Kinds whose params carry a model_variant. Everything else stays NULL.
_LOCAL_MODEL_KINDS = ("image_local", "img2img", "inpaint", "outpaint", "control_local")


def run(cur: sqlite3.Cursor) -> None:
    add_column(cur, "job", "model_key", "TEXT")
    add_index(cur, "ix_job_model_key", "job", "model_key")
    add_index(cur, "ix_job_dequeue", "job", "status, priority DESC, id ASC")

    if not table_exists(cur, "job"):
        return

    # Backfill from params_json. The LIKE keeps this off rows that cannot match
    # rather than decoding every job ever run.
    placeholders = ",".join("?" * len(_LOCAL_MODEL_KINDS))
    cur.execute(
        f"SELECT id, params_json FROM job "
        f"WHERE model_key IS NULL AND kind IN ({placeholders}) "
        f"AND params_json LIKE '%model_variant%'",
        _LOCAL_MODEL_KINDS,
    )
    for jid, raw in cur.fetchall():
        try:
            variant = json.loads(raw or "{}").get("model_variant")
        except (ValueError, TypeError):
            continue  # a malformed params blob costs that row its key, nothing more
        if isinstance(variant, str) and variant:
            cur.execute("UPDATE job SET model_key = ? WHERE id = ?", (variant, jid))
