"""Pick defaults that suit the machine this is actually running on.

The constants in `generators/base.py` were tuned for one 16 GB card. On 8 GB they
produce out-of-memory errors; on 24 GB they leave most of the card idle and
generate needlessly small images. Neither is a good first impression for someone
who just cloned the repo.

So: detect the VRAM once at startup, choose a profile, and let the user override
it. **The choice is always visible.** Silent auto-configuration is worse than
none, because when it guesses wrong there is nothing to look at and no obvious
thing to change.

The numbers below are a starting shape, not measurements. They are deliberately
conservative — an image that is smaller than it could be is a much better failure
than a job that dies at 90%.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import log

logger = log.get("hardware")


@dataclass(frozen=True)
class Profile:
    """Everything that should scale with the size of the accelerator."""
    name: str
    label: str
    min_vram_gb: float
    max_side: int
    tier_mp: dict[str, float] = field(default_factory=dict)
    max_batch: int = 8


PROFILES: tuple[Profile, ...] = (
    Profile("cpu", "CPU only", 0.0, 768,
            {"Draft": 0.15, "Standard": 0.25, "High": 0.35}, max_batch=1),
    Profile("8gb", "8 GB", 7.0, 1024,
            {"Draft": 0.20, "Standard": 0.40, "High": 0.60}, max_batch=8),
    Profile("12gb", "12 GB", 11.0, 1152,
            {"Draft": 0.25, "Standard": 0.50, "High": 0.75}, max_batch=8),
    Profile("16gb", "16 GB", 15.0, 1280,
            {"Draft": 0.30, "Standard": 0.60, "High": 0.92}, max_batch=8),
    Profile("24gb", "24 GB or more", 23.0, 1664,
            {"Draft": 0.40, "Standard": 0.80, "High": 1.30}, max_batch=8),
)

FALLBACK = PROFILES[0]


def profile_for(vram_gb: float | None) -> Profile:
    """The largest profile this card can carry.

    Unknown VRAM returns the 16 GB profile rather than the CPU one: an unprobed
    machine is far more likely to be a normal GPU box than a CPU-only one, and
    the previous hardcoded behaviour was exactly this profile.
    """
    if vram_gb is None:
        return by_name("16gb")
    best = FALLBACK
    for p in PROFILES:
        if vram_gb >= p.min_vram_gb:
            best = p
    return best


def by_name(name: str) -> Profile:
    for p in PROFILES:
        if p.name == name:
            return p
    return FALLBACK


def active() -> Profile:
    """The profile in force: the user's override, or the detected one.

    Read fresh each time rather than cached at import, because the device probe
    finishes on a background thread after startup and an override can be changed
    in Settings without a restart.
    """
    from .backends.local import _device_info
    from .config import settings

    override = (settings.hardware_profile or "").strip().lower()
    if override and override != "auto":
        chosen = by_name(override)
        if chosen.name != override:
            logger.warning("unknown HARDWARE_PROFILE %r; using %s", override, chosen.name)
        return chosen
    _, vram = _device_info()
    return profile_for(vram)


def describe() -> dict:
    """What was chosen and why, for the settings page.

    Showing the reasoning is the point. A user whose 12 GB card was detected as
    8 GB needs to be able to see that, not just experience smaller images.
    """
    from .backends.local import _device_info
    from .config import settings

    device, vram = _device_info()
    p = active()
    override = (settings.hardware_profile or "").strip().lower()
    return {
        "device": device or "not detected",
        "vram_gb": vram,
        "profile": p.name,
        "label": p.label,
        "source": "override" if override and override != "auto" else "detected",
        # These are configuration, not hardware-profile recommendations. The
        # old profile values were never applied and could contradict the actual
        # loader (for example profile=nunchaku while LOCAL_QUANT=4bit).
        "quant": settings.local_quant,
        "offload": settings.local_offload,
        "max_side": p.max_side,
        "tier_mp": p.tier_mp,
        "max_batch": p.max_batch,
        "available": [{"name": q.name, "label": q.label} for q in PROFILES],
    }
