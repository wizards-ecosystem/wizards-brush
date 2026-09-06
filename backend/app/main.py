"""FastAPI entry point: wires routers, serves /output files and the built SPA."""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import enrichment, log, notify
from .config import ROOT, settings
from .db import (
    acquire_process_lock,
    init_db,
    prune_job_history,
    reconcile_orphans,
    release_process_lock,
    remote_orphans,
    sweep_orphan_files,
)
from .generators.base import setup_hf_env
from .queue import resume_queued, start_all
from .routers import (
    assets,
    collections,
    grid,
    images,
    jobs,
    library,
    loras,
    system,
    tools,
    videos,
    wildcards,
)
from .routers import (
    settings as settings_router,
)
from .security import BrowserSecurityMiddleware, allowed_origins, browser_session_token
from .version import get_version

log.setup()
logger = log.get("startup")


def _warm_and_sweep() -> None:
    """Background startup work: sweep stale temp files and pre-load the local model
    so the first generation isn't a cold-start stall. Runs off the event loop."""
    try:
        removed = sweep_orphan_files()
        if removed:
            logger.info("swept %d orphaned temp file(s)", removed)
    except Exception as e:  # noqa: BLE001
        logger.warning("orphan sweep skipped: %s", e)
    try:
        queued = enrichment.backfill_hashes()
        if queued:
            logger.info("queued %d asset(s) for content hashing", queued)
    except Exception as e:  # noqa: BLE001 — backfill is catch-up work, never blocking
        logger.warning("hash backfill skipped: %s", e)
    try:
        pruned = prune_job_history()
        if pruned:
            logger.info("pruned %d old job row(s)", pruned)
    except Exception as e:  # noqa: BLE001 — housekeeping never blocks startup
        logger.warning("job history prune skipped: %s", e)
    # Detect the GPU once, here, where paying the torch import is already the
    # plan. health() then reads a cache instead of importing anything, so the
    # settings page never pulls the heavy stack into the request path.
    if settings.probe_device:
        try:
            from .backends.local import probe_device

            name, vram = probe_device()
            logger.info("local device: %s",
                        f"{name} ({vram} GB)" if name else "none (CPU only)")
        except Exception as e:  # noqa: BLE001 — a machine with no GPU is normal
            logger.warning("device probe skipped: %s", e)
    # Same reasoning, same thread: resolving the quantization backend can import
    # torch and run a subprocess preflight, and GET /api/models must never pay
    # for that on the event loop.
    try:
        from .generators.registry import probe_lora_support

        blocked = [name for name, ok in probe_lora_support().items() if not ok]
        if blocked:
            logger.info("LoRA picker hidden for %s: their quantization backend "
                        "cannot merge adapters", ", ".join(sorted(blocked)))
    except Exception as e:  # noqa: BLE001 — a capability probe never blocks startup
        logger.warning("lora capability probe skipped: %s", e)
    if not settings.warm_up:
        return
    try:
        from .generators import local_image

        logger.info("warming up local image pipeline …")
        local_image.warm_up()
    except Exception as e:  # noqa: BLE001
        logger.warning("warm-up skipped: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading

    setup_hf_env()
    # NOTIFY_IDLE_SECONDS lives in settings; the watcher is built at import time
    # with a module default. Without this the setting is documented and inert.
    notify.configure()
    # Before anything touches the database: refuse to start if another instance
    # already holds it. Two processes would run two job lanes over one queue.
    acquire_process_lock()
    init_db()
    abandoned_remote = remote_orphans()
    n = reconcile_orphans()
    if n:
        logger.info("canceled %d job(s) interrupted mid-run by the restart", n)
    if abandoned_remote:
        import asyncio

        from .remote_gpu_client import cancel_remote_identity

        reclaimed = await asyncio.gather(*(
            asyncio.to_thread(cancel_remote_identity, token, client_id)
            for _job_id, token, client_id in abandoned_remote
        ))
        logger.info("reclaimed %d/%d interrupted A100 job(s)",
                    sum(bool(done) for done in reclaimed), len(abandoned_remote))
    start_all()
    # After the lanes exist: anything still queued never started, so it survives
    # the restart instead of being thrown away with the one job that was running.
    resumed = await resume_queued()
    if resumed:
        logger.info("resumed %d queued job(s)", resumed)
    threading.Thread(target=_warm_and_sweep, daemon=True).start()
    try:
        yield
    finally:
        release_process_lock()


app = FastAPI(title="The Wizard's Brush", version=get_version(), lifespan=lifespan)

# CORS is deliberately NOT "*".
#
# The SPA is served from the same origin as the API, so it never needs a CORS
# grant; the only real consumer is the Vite dev server on another port. With
# allow_origins=["*"] and API_TOKEN empty (the default), any page the user
# happened to have open could POST /api/assets/bulk-delete at localhost:8000 and
# wipe the gallery. Same-origin requests carry no Origin header and are
# unaffected by any of this, so the tighter default costs nothing.
def _allowed_origins() -> list[str]:
    return allowed_origins(settings.cors_origins)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


def _token_ok(supplied: str) -> bool:
    """Constant-time API-token check, shared by the HTTP and WS auth layers."""
    return secrets.compare_digest(supplied or "", settings.api_token)


# Opt-in token auth (set API_TOKEN in .env). CLI callers use a header. Browsers
# exchange that header once for server-set HttpOnly cookies: one scoped to /api
# and one to /files for media elements. Reusable credentials stay out of URLs,
# script-readable storage, access logs, downloads, and markup.
@app.middleware("http")
async def api_auth(request, call_next):
    if settings.api_token:
        clearing_browser_session = (
            request.method == "DELETE" and request.url.path == "/api/auth/session"
        )
        api_header = request.headers.get("x-api-token")
        api_credential_ok = (
            _token_ok(api_header)
            if api_header is not None
            else secrets.compare_digest(
                request.cookies.get("wb_api_token") or "",
                browser_session_token(settings.api_token, "api"),
            )
        )
        if (request.url.path.startswith("/api")
                and not clearing_browser_session
                and not api_credential_ok):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        media_cookie_ok = secrets.compare_digest(
            request.cookies.get("wb_media_token") or "",
            browser_session_token(settings.api_token, "media"),
        )
        if request.url.path.startswith("/files/") and not media_cookie_ok:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


app.add_middleware(BrowserSecurityMiddleware, settings=settings)

# API routers (grid after images — it reuses images' registered handlers)
for r in (system.router, settings_router.router, images.router, videos.router,
          tools.router, jobs.router, assets.router, library.router, wildcards.router,
          loras.router, collections.router, grid.router):
    app.include_router(r, prefix="/api")

# Cache policy for generated media.
#
# Every file we serve under /files is immutable: names carry a millisecond stamp
# plus a counter, so a given URL always means the same bytes. Without a policy
# the browser revalidates each thumbnail on every gallery scroll, which is a
# round trip per tile for content that provably cannot have changed.
#
# 404s are cached briefly too. A thumbnail that does not exist will not exist a
# second later either, and a broken tile in a long gallery would otherwise
# re-request on every render.
_IMMUTABLE_MAX_AGE = 86_400        # a day
_MISS_MAX_AGE = 300                # five minutes


@app.middleware("http")
async def media_cache_control(request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/files/"):
        return response
    if response.status_code == 404:
        response.headers.setdefault("Cache-Control", f"public, max-age={_MISS_MAX_AGE}")
    elif 200 <= response.status_code < 300:
        response.headers.setdefault(
            "Cache-Control",
            f"{'private' if settings.api_token else 'public'}, "
            f"max-age={_IMMUTABLE_MAX_AGE}, immutable",
        )
    return response


# Serve generated media directly. Each media subdir is mounted individually so
# the DB and runtime_settings.json (which can hold the Remote GPU secret) in the output root
# are never reachable over HTTP.
settings.ensure_dirs()
for sub, d in (("images", settings.images_dir), ("videos", settings.videos_dir),
               ("thumbs", settings.thumbs_dir), ("uploads", settings.uploads_dir)):
    app.mount(f"/files/{sub}", StaticFiles(directory=str(d)), name=f"files-{sub}")

# Serve the built frontend (after `npm run build`); falls back to a hint in dev.
_DIST = ROOT / "frontend" / "dist"
if _DIST.exists():
    _DIST_RESOLVED = _DIST.resolve()  # once, not per request

    # Two opposite caching rules, because these are two different kinds of file.
    #
    # Everything under /assets is content-hashed by Vite: the name changes when
    # the bytes do, so it can be cached forever and never revalidated.
    #
    # index.html is the opposite: its name never changes and its whole job is to
    # name the current bundle. Served with only ETag/Last-Modified — which is
    # what FileResponse does by default — a browser is free to reuse it from
    # cache without asking, so a plain refresh keeps loading the PREVIOUS
    # bundle and the app looks like it never updated. `no-cache` does not mean
    # "do not store", it means "revalidate before use": with the ETag that is a
    # 304 and a few bytes, and the user always gets the deployed build.
    _IMMUTABLE = "public, max-age=31536000, immutable"
    _REVALIDATE = "no-cache"

    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="spa-assets")

    @app.middleware("http")
    async def _spa_cache_headers(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets/"):
            response.headers.setdefault("cache-control", _IMMUTABLE)
        elif not path.startswith(("/api", "/files")):
            response.headers["cache-control"] = _REVALIDATE
        return response

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        # Resolve and confine to dist/ so "../" can't escape into the repo.
        try:
            candidate = (_DIST / full_path).resolve()
            candidate.relative_to(_DIST_RESOLVED)
        except (ValueError, OSError):
            candidate = None
        if full_path and candidate and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
else:
    @app.get("/", include_in_schema=False)
    async def dev_root():
        return JSONResponse({
            "app": "The Wizard's Brush",
            "note": "Frontend not built. Run `npm run dev` in ./frontend (port 5173), "
                    "or `bash scripts/start.sh` to build + serve here.",
            "api_docs": "/docs",
        })
