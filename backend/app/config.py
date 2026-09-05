"""Project-wide settings, loaded once from the root `.env`.

A single source of truth shared by every router and generator. Mutable runtime
overrides (e.g. the remote GPU URL pasted in the UI) live in `runtime_settings.json`
next to the output dir, layered on top of the `.env` defaults.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, ValidationError
from pydantic_core import PydanticUndefined
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = three levels up from this file (backend/app/config.py -> repo).
ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
_OVERRIDE_LOCK = threading.RLock()
_OVERRIDE_WARNING = ""
_OVERRIDE_SAVE_BLOCKED = False
if ENV_FILE.exists() or ENV_FILE.is_symlink():
    try:
        ENV_FILE.resolve().relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeError(f".env must be a real project-local file; got {ENV_FILE.resolve()}") from exc
    if os.name == "posix" and stat.S_IMODE(ENV_FILE.stat().st_mode) != 0o600:
        try:
            os.chmod(ENV_FILE, 0o600)
        except OSError as exc:
            raise RuntimeError(f".env permissions could not be restricted to 0600: {exc}") from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # secrets / models — best-in-class, sized to each device.
    hf_token: str = Field(default="", alias="HF_TOKEN")
    # Local 16 GB card: Z-Image-Turbo (fast, fits 16 GB with 4-bit + offload).
    local_image_model: str = Field(default="Tongyi-MAI/Z-Image-Turbo", alias="LOCAL_IMAGE_MODEL")
    # Optional local "Quality" variant: Z-Image base (CFG model, more steps,
    # richer detail). Empty by default so a clean install downloads one image
    # checkpoint instead of a multi-model evaluation suite.
    local_image_model_hq: str = Field(default="", alias="LOCAL_IMAGE_MODEL_HQ")
    # Current compact challenger: a native Diffusers pipeline that the publisher
    # validates on 4070-class cards at roughly 13 GB VRAM.  Kept in its own slot
    # because its distilled 4-step / CFG-1 recipe is not compatible with the
    # older FLUX.1 compatibility slot below.
    local_image_model_klein: str = Field(default="", alias="LOCAL_IMAGE_MODEL_KLEIN")
    # Optional legacy-family slots. Empty by default: the two official Z-Image
    # checkpoints are the strongest practical choices on the target 16 GB card,
    # and a clean install should not silently download another 60+ GB of older
    # FLUX-family weights. Overrides still appear in the picker for users who
    # deliberately keep a broader private library.
    local_image_model_chroma: str = Field(default="", alias="LOCAL_IMAGE_MODEL_CHROMA")
    # Local SDXL slot. Any SDXL checkpoint: ~6.9 GB in bf16, so it needs neither
    # quantization nor offload on a 16 GB card, and it is the cheapest way to get
    # local img2img / inpaint / outpaint. Empty by default — point it at whatever
    # checkpoint you use and the option appears in the picker.
    local_image_model_sdxl: str = Field(default="", alias="LOCAL_IMAGE_MODEL_SDXL")
    local_image_model_flux: str = Field(default="", alias="LOCAL_IMAGE_MODEL_FLUX")
    # ---- Remote 80 GB GPU: curated image models + optional custom slots -------
    # The quality slot. Qwen-Image-2512 is the best model that fits an A100 at
    # full bf16 (57.7 GB) — FLUX.2-dev is 112 GB in bf16 (64.4 transformer +
    # 48.0 text encoder) and would have to be crushed to NF4 to fit, which loses
    # more than it gains over a full-precision Qwen. Apache-2.0 and ungated.
    a100_image_model: str = Field(default="Qwen/Qwen-Image-2512", alias="A100_IMAGE_MODEL")
    # Strongest single-A100 comparison that is both straightforward to deploy
    # and small enough to run at its published bf16 recipe.
    # HiDream uses its official custom Pixel-DiT runner rather than Diffusers;
    # remote_gpu.py detects this dedicated slot and loads that runner explicitly.
    a100_image_model_hidream: str = Field(
        default="HiDream-ai/HiDream-O1-Image", alias="A100_IMAGE_MODEL_HIDREAM")
    # Generic optional A100 image slot for an additional Diffusers model.
    # Anything in diffusers format that fits 80 GB; a FLUX.2-klein 9B checkpoint
    # is ~34.8 GB in bf16 (18.2 transformer + 16.4 text encoder) and runs
    # unquantized with room to spare. Empty by default — the picker only appears
    # when a second model is configured, so the app never offers the worker a
    # model it does not have.
    a100_image_model_alt: str = Field(default="", alias="A100_IMAGE_MODEL_ALT")
    # Remote image editing (1–3 input images).
    qwen_edit_model: str = Field(default="Qwen/Qwen-Image-Edit-2511", alias="QWEN_EDIT_MODEL")
    # Video on the A100. TI2V-5B is the best that doesn't tank the remote workflow
    # (A14B is 126 GB/model — impractical to download per session).
    video_model: str = Field(default="Wan-AI/Wan2.2-TI2V-5B-Diffusers", alias="VIDEO_MODEL")
    # Optional second video engine on the A100 (empty = disabled). Its Tencent
    # community license has geographic and use restrictions, so it must never
    # be enabled by a jurisdiction-agnostic public default.
    hunyuan_video_model: str = Field(default="", alias="HUNYUAN_VIDEO_MODEL")
    # LTX-2: the only open-weights family with synced audio. Left empty by
    # default because the checkpoints are 150-200 GB and will not fit on a
    # standard 80 GB runtime alongside the image and Wan models.
    ltx_video_model: str = Field(default="", alias="LTX_VIDEO_MODEL")
    # Model-matched Lightning distill LoRAs for A100 speed mode. Generation and
    # editing need separate adapters: silently applying the old Qwen-Image LoRA
    # to either 2512 model produced a superficially successful but invalid setup.
    image_lightning_lora: str = Field(
        default="lightx2v/Qwen-Image-2512-Lightning", alias="IMAGE_LIGHTNING_LORA")
    edit_lightning_lora: str = Field(
        default="lightx2v/Qwen-Image-Edit-2511-Lightning", alias="EDIT_LIGHTNING_LORA")
    video_lightning_lora: str = Field(default="", alias="VIDEO_LIGHTNING_LORA")

    # local image model perf
    local_quant: str = Field(
        default="4bit", alias="LOCAL_QUANT")  # nunchaku | 4bit | fp8 | none
    local_offload: bool = Field(default=True, alias="LOCAL_OFFLOAD")
    # Preload the local model at startup so the first generation isn't a cold stall.
    warm_up: bool = Field(default=True, alias="WARM_UP")
    # Detect the GPU at startup, in the background. Separate from warm_up on
    # purpose: hardware-adaptive defaults need to know the card even when the
    # user does not want a pipeline pre-loaded. Off in tests, which must never
    # import torch.
    probe_device: bool = Field(default=True, alias="PROBE_DEVICE")
    # How ControlNet behaves when LOCAL_OFFLOAD is on.
    #   "refuse"  — today's behaviour: fail early with a clear message.
    #   "rehook"  — re-own device placement so both can run. See generators/offload.py.
    # Defaults to refuse because the failure mode is an uncatchable CUDA abort
    # and the alternative path cannot be verified without a real device.
    controlnet_offload: str = Field(default="refuse", alias="CONTROLNET_OFFLOAD")
    # Which hardware profile to use: "auto" detects from VRAM, or name one
    # explicitly (cpu, 8gb, 12gb, 16gb, 24gb). See backend/app/hardware.py.
    hardware_profile: str = Field(default="auto", alias="HARDWARE_PROFILE")
    # Rung 3 of the enrichment ladder: a CLIP vector per asset, enabling gallery
    # search by meaning rather than by caption keyword. Off by default — it is a
    # further model download and most of the value is in captions and tags.
    enrich_embeddings: bool = Field(default=False, alias="ENRICH_EMBEDDINGS")
    embed_model: str = Field(default="openai/clip-vit-base-patch32", alias="EMBED_MODEL")
    # Optional POST hooks. Empty = off. The queue must stay idle for
    # notify_idle_seconds before it counts as finished — without that debounce a
    # "queue empty" hook fires between every pair of jobs.
    notify_queue_start_url: str = Field(default="", alias="NOTIFY_QUEUE_START_URL")
    notify_queue_idle_url: str = Field(default="", alias="NOTIFY_QUEUE_IDLE_URL")
    notify_job_done_url: str = Field(default="", alias="NOTIFY_JOB_DONE_URL")
    notify_idle_seconds: float = Field(default=5.0, alias="NOTIFY_IDLE_SECONDS")
    # First-block cache threshold (0 = off). Helps at >=20 steps; hurts <10-step Turbo.
    fbcache_threshold: float = Field(default=0.0, alias="FBCACHE_THRESHOLD")
    # Emit a live latent preview every N denoising steps (0 = off).
    preview_every: int = Field(default=2, alias="PREVIEW_EVERY")
    # SageAttention backend on the remote GPU pipes. Shipped to the remote runner
    # by `make remote-gpu`; local generation does not use it. Kept here (rather than
    # env-only on the remote side) so one .env is the single source of truth —
    # it used to default to a different value there with no way to override.
    enable_sage_attention: bool = Field(default=False, alias="ENABLE_SAGE_ATTENTION")

    # Gallery enrichment (async worker; see enrichment.py).
    enrich_captions: bool = Field(default=True, alias="ENRICH_CAPTIONS")

    # Experimental local ControlNet.
    enable_controlnet: bool = Field(default=False, alias="ENABLE_CONTROLNET")
    controlnet_model: str = Field(
        default="alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union-2.1", alias="CONTROLNET_MODEL")

    # Remote GPU connection. The old COLAB_* names remain accepted only so an
    # existing private .env or runtime-settings file does not stop working after
    # the platform-neutral rename. New configuration and the public API use
    # REMOTE_GPU_* exclusively.
    remote_gpu_base_url: str = Field(
        default="", alias="REMOTE_GPU_BASE_URL",
        validation_alias=AliasChoices("REMOTE_GPU_BASE_URL", "COLAB_BASE_URL"),
    )
    remote_gpu_shared_secret: str = Field(
        default="change-me-to-anything", alias="REMOTE_GPU_SHARED_SECRET",
        validation_alias=AliasChoices("REMOTE_GPU_SHARED_SECRET", "COLAB_SHARED_SECRET"),
    )

    # app
    # Optional bearer token for /api/* — empty disables auth (single-user LAN default).
    # Extra browser origins allowed to call /api/*, comma-separated. The Vite dev
    # server and same-origin requests are always allowed. Wildcards are ignored:
    # this app can run without a token, so browser origins must be explicit.
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")
    api_token: str = Field(default="", alias="API_TOKEN")
    # File export privacy. Reproducibility metadata and the standardized AI
    # provenance declaration are separate choices because their audiences and
    # disclosure consequences differ.
    embed_metadata: bool = Field(default=True, alias="EMBED_METADATA")
    embed_provenance: bool = Field(default=True, alias="EMBED_PROVENANCE")
    # Backend logging verbosity (DEBUG/INFO/WARNING/ERROR). An OS env var beats
    # the .env value (pydantic-settings precedence), which is what tests rely on.
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    output_dir: str = Field(default="./output", alias="OUTPUT_DIR")
    # Drop LoRA .safetensors here; they are listed by GET /api/loras.
    lora_dir: str = Field(default="./models/loras", alias="LORA_DIR")
    hf_home: str = Field(default="./models/huggingface", alias="HF_HOME")
    weights_dir: str = Field(default="./models/weights", alias="WEIGHTS_DIR")
    runtime_dir: str = Field(default="./.runtime", alias="RUNTIME_DIR")

    # ---- derived paths -------------------------------------------------
    @staticmethod
    def _inside_project(raw: str, label: str) -> Path:
        """Resolve a mutable path and reject anything outside the checkout.

        Environment variables have higher precedence than `.env`; validating at
        this boundary prevents a forgotten host-level OUTPUT_DIR/HF_HOME from
        silently defeating the self-contained runtime contract.
        """
        value = Path(raw).expanduser()
        resolved = value.resolve() if value.is_absolute() else (ROOT / value).resolve()
        try:
            resolved.relative_to(ROOT)
        except ValueError as exc:
            raise ValueError(f"{label} must stay inside {ROOT}; got {resolved}") from exc
        return resolved

    @property
    def output_path(self) -> Path:
        return self._inside_project(self.output_dir, "OUTPUT_DIR")

    @property
    def images_dir(self) -> Path:
        return self._inside_project(str(self.output_path / "images"), "image output")

    @property
    def videos_dir(self) -> Path:
        return self._inside_project(str(self.output_path / "videos"), "video output")

    @property
    def thumbs_dir(self) -> Path:
        return self._inside_project(str(self.output_path / "thumbs"), "thumbnail output")

    @property
    def uploads_dir(self) -> Path:
        return self._inside_project(str(self.output_path / "uploads"), "upload storage")

    @property
    def db_path(self) -> Path:
        return self._inside_project(str(self.output_path / "gen.db"), "database")

    @property
    def runtime_overrides_path(self) -> Path:
        return self._inside_project(
            str(self.output_path / "runtime_settings.json"), "runtime settings"
        )

    @property
    def runtime_path(self) -> Path:
        return self._inside_project(self.runtime_dir, "RUNTIME_DIR")

    @property
    def temp_path(self) -> Path:
        return self._inside_project(str(self.runtime_path / "tmp"), "runtime temp")

    @property
    def hf_home_path(self) -> Path:
        return self._inside_project(self.hf_home, "HF_HOME")

    @property
    def hf_hub_path(self) -> Path:
        return self._inside_project(str(self.hf_home_path / "hub"), "Hugging Face hub cache")

    @property
    def weights_path(self) -> Path:
        return self._inside_project(self.weights_dir, "WEIGHTS_DIR")

    @property
    def loras_dir(self) -> Path:
        """Where local LoRA .safetensors live. Outside output/ on purpose: these
        are curated inputs, not generated artefacts, so `make clean-gens` and the
        orphan sweep must never touch them."""
        return self._inside_project(self.lora_dir, "LORA_DIR")

    def ensure_dirs(self) -> None:
        for d in (self.images_dir, self.videos_dir, self.thumbs_dir, self.uploads_dir,
                  self.loras_dir, self.hf_home_path, self.weights_path, self.temp_path):
            d.mkdir(parents=True, exist_ok=True)

    def apply_process_environment(self) -> None:
        """Enforce containment even when Python bypasses the shell entrypoints.

        The shell environment also covers package-manager commands. This is the
        backend-side safety net for libraries that consult HOME/XDG or create a
        compiler/model cache during a first-use import.
        """
        # Must exist before the first torch import. Expandable segments let the
        # allocator grow allocations instead of marooning unusable fragments as
        # aspect ratios and quality tiers change. Respect an explicit operator
        # override rather than silently replacing it.
        os.environ.setdefault("PYTORCH_ALLOC_CONF", "expandable_segments:True")
        cache = self._inside_project(str(self.runtime_path / "cache"), "runtime cache")
        config = self._inside_project(str(self.runtime_path / "config"), "runtime config")
        data = self._inside_project(str(self.runtime_path / "data"), "runtime data")
        state = self._inside_project(str(self.runtime_path / "state"), "runtime state")
        paths = {
            "HOME": self.runtime_path / "home",
            "XDG_CACHE_HOME": cache / "xdg",
            "XDG_CONFIG_HOME": config / "xdg",
            "XDG_DATA_HOME": data / "xdg",
            "XDG_STATE_HOME": state / "xdg",
            "UV_CACHE_DIR": cache / "uv",
            "UV_PYTHON_INSTALL_DIR": self.runtime_path / "python",
            "UV_TOOL_DIR": self.runtime_path / "uv-tools",
            "UV_TOOL_BIN_DIR": self.runtime_path / "bin",
            "UV_PROJECT_ENVIRONMENT": ROOT / ".venv",
            "PIP_CACHE_DIR": cache / "pip",
            "PYTHONUSERBASE": self.runtime_path / "python-user",
            "PYTHONPYCACHEPREFIX": cache / "python-bytecode",
            "RUFF_CACHE_DIR": cache / "ruff",
            "NPM_CONFIG_CACHE": cache / "npm",
            "NPM_CONFIG_PREFIX": self.runtime_path / "npm-prefix",
            "COREPACK_HOME": cache / "corepack",
            "PLAYWRIGHT_BROWSERS_PATH": cache / "playwright",
            "ELECTRON_CACHE": cache / "electron",
            "NODE_COMPILE_CACHE": cache / "node-compile",
            "HF_HOME": self.hf_home_path,
            "HF_HUB_CACHE": self.hf_hub_path,
            "HF_XET_CACHE": self.hf_home_path / "xet",
            "HF_ASSETS_CACHE": self.hf_home_path / "assets",
            "HF_MODULES_CACHE": self.hf_home_path / "modules",
            "HUGGINGFACE_HUB_CACHE": self.hf_hub_path,
            "SENTENCE_TRANSFORMERS_HOME": ROOT / "models" / "sentence-transformers",
            "TORCH_HOME": ROOT / "models" / "torch",
            "TORCH_EXTENSIONS_DIR": cache / "torch-extensions",
            "TORCHINDUCTOR_CACHE_DIR": cache / "torch-inductor",
            "TRITON_CACHE_DIR": cache / "triton",
            "CUDA_CACHE_PATH": cache / "cuda",
            "NUMBA_CACHE_DIR": cache / "numba",
            "MPLCONFIGDIR": config / "matplotlib",
            "IMAGEIO_USERDIR": data / "imageio",
            "KERAS_HOME": ROOT / "models" / "keras",
            "ONNX_HOME": ROOT / "models" / "onnx",
            "CARGO_HOME": self.runtime_path / "cargo",
            "RUSTUP_HOME": self.runtime_path / "rustup",
            "CCACHE_DIR": cache / "ccache",
            "SCCACHE_DIR": cache / "sccache",
            "IPYTHONDIR": config / "ipython",
            "JUPYTER_CONFIG_DIR": config / "jupyter",
            "WANDB_DIR": state / "wandb",
            "WANDB_CACHE_DIR": cache / "wandb",
            "WANDB_CONFIG_DIR": config / "wandb",
            "OUTPUT_DIR": self.output_path,
            "LORA_DIR": self.loras_dir,
            "WEIGHTS_DIR": self.weights_path,
            "RUNTIME_DIR": self.runtime_path,
            "TMPDIR": self.temp_path,
            "TMP": self.temp_path,
            "TEMP": self.temp_path,
        }
        for name, path in paths.items():
            safe_path = self._inside_project(str(path), name)
            safe_path.mkdir(parents=True, exist_ok=True)
            os.environ[name] = str(safe_path)
        files = {
            "PIP_CONFIG_FILE": config / "pip" / "pip.conf",
            "NPM_CONFIG_USERCONFIG": config / "npm" / "npmrc",
        }
        for name, path in files.items():
            safe_path = self._inside_project(str(path), name)
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            safe_path.touch()
            os.environ[name] = str(safe_path)

    # ---- runtime overrides (set from the Settings UI) ------------------
    def load_overrides(self) -> dict:
        global _OVERRIDE_SAVE_BLOCKED, _OVERRIDE_WARNING
        p = self.runtime_overrides_path
        with _OVERRIDE_LOCK:
            if not p.exists():
                return {}
            try:
                if os.name == "posix" and stat.S_IMODE(p.stat().st_mode) != 0o600:
                    os.chmod(p, 0o600)
                loaded = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("top-level value is not an object")
                return loaded
            except Exception as exc:  # noqa: BLE001 — preserve, report, and use defaults
                backup = p.with_name(f"{p.stem}.corrupt-{time.time_ns()}{p.suffix}")
                try:
                    os.replace(p, backup)
                    if os.name == "posix":
                        os.chmod(backup, 0o600)
                except FileNotFoundError:  # another reader already quarantined it
                    return {}
                except OSError as move_error:
                    _OVERRIDE_SAVE_BLOCKED = True
                    _OVERRIDE_WARNING = (
                        f"Runtime settings could not be read ({exc}) or preserved ({move_error}). "
                        "Fix or remove the file before saving settings."
                    )
                    return {}
                _OVERRIDE_WARNING = (
                    f"Unreadable runtime settings were preserved as {backup.name}; "
                    "defaults are active until you save replacements."
                )
                _OVERRIDE_SAVE_BLOCKED = False
                return {}

    def save_overrides(self, data: dict) -> None:
        with _OVERRIDE_LOCK:
            if _OVERRIDE_SAVE_BLOCKED:
                raise RuntimeError(
                    "runtime settings are unreadable and could not be preserved; "
                    "fix or remove the file before saving"
                )
            self.ensure_dirs()
            payload = json.dumps(data, indent=2).encode("utf-8")
            # Replace atomically: losing power between truncate() and write() used
            # to turn every saved runtime setting into a silent empty fallback.
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.runtime_overrides_path.parent,
                prefix=".runtime-settings-", suffix=".tmp", delete=False,
            ) as tmp:
                if os.name == "posix":
                    os.fchmod(tmp.fileno(), 0o600)
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
                pending = Path(tmp.name)
            try:
                os.replace(pending, self.runtime_overrides_path)
                if os.name == "posix":
                    os.chmod(self.runtime_overrides_path, 0o600)
            finally:
                pending.unlink(missing_ok=True)

    @property
    def override_warning(self) -> str:
        """Recoverable persistence warning retained for the current process."""
        return _OVERRIDE_WARNING

    def effective_bool(self, name: str) -> bool:
        """Runtime override for one declared boolean setting."""
        fallback = bool(getattr(self, name))
        value = self.load_overrides().get(name, fallback)
        return value if isinstance(value, bool) else fallback

    @property
    def effective_remote_gpu_url(self) -> str:
        """UI-set remote GPU URL wins over .env; trailing slash is stripped.

        `colab_base_url` is read as a migration fallback only.  We intentionally
        leave it out of new writes so a saved setting naturally converges on the
        provider-neutral name.
        """
        overrides = self.load_overrides()
        url = (
            overrides.get("remote_gpu_base_url")
            or overrides.get("colab_base_url")
            or self.remote_gpu_base_url
        )
        return url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    s = _settings_with_invalid_values_defaulted()
    s.apply_process_environment()
    s.ensure_dirs()
    return s


def _settings_with_invalid_values_defaulted() -> Settings:
    """Load all valid configuration while defaulting only malformed fields.

    Pydantic normally rejects the entire settings object for one value such as
    ``PREVIEW_EVERY=not-a-number``. Because settings are imported at process
    startup, that prevents the server—and therefore Diagnosis—from opening at
    all. Init values have higher priority than environment and dotenv sources,
    so retry with the declared default for each field Pydantic identified.

    This catches validation only. Path-containment checks deliberately happen
    later and still fail closed; a bad OUTPUT_DIR must never be silently turned
    into a different storage location.
    """
    overrides: dict[str, Any] = {}
    fields = {
        str(field.alias or name): field
        for name, field in Settings.model_fields.items()
    }
    fields.update(dict(Settings.model_fields))

    while True:
        try:
            return Settings(**overrides)
        except ValidationError as exc:
            added = False
            for error in exc.errors(include_url=False):
                loc = error.get("loc") or ()
                name = str(loc[0]) if loc else ""
                field = fields.get(name)
                alias = str(field.alias or name) if field is not None else name
                if field is None or alias in overrides:
                    raise
                default = field.get_default(call_default_factory=True)
                if default is PydanticUndefined:
                    raise
                overrides[alias] = default
                raw = "<redacted>" if any(
                    marker in alias for marker in ("TOKEN", "SECRET", "PASSWORD")
                ) else repr(error.get("input"))[:120]
                print(
                    f"Configuration warning: {alias}={raw} is invalid "
                    f"({error.get('msg', 'validation failed')}); using default {default!r}.",
                    file=sys.stderr,
                )
                added = True
            if not added:
                raise


settings = get_settings()
