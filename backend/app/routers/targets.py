"""Targets over HTTP: what each place can run, and the routing policy.

`GET /api/targets` is read-only and never touches a provider: the Remote GPU
part is the worker's last health answer (refreshed when older than 15 s, as
every gate does). Changing the policy is a separate, named action.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from ..studio import targets
from .common import error_responses

router = APIRouter(tags=["targets"])


class PolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_enabled: bool | None = None
    auto_may_use_remote: bool | None = None
    send_inputs_to_remote: bool | None = None


@router.get("/targets")
async def read_targets() -> dict[str, Any]:
    """Capability reports for this machine and the Remote GPU, the routing
    policy, and the capability revision a client keeps with its controls."""
    from .job_api import remote_state

    state = await remote_state()
    return targets.reports(connected=bool(state.get("connected")))


@router.put("/targets/policy", responses=error_responses(400))
async def update_policy(body: PolicyUpdate) -> dict[str, bool]:
    """Change what Auto may do and whether inputs may go to a remote worker.
    Changing it never starts, stops or rents anything."""
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    return targets.save_policy(changes)
