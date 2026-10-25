"""Hugging Face and Civitai: parse a link, inspect it, fetch bytes safely.

Inspection reads provider metadata only (repository file lists with their LFS
hashes, Civitai versions and files) and never a weight byte. It presents what
the link could mean, every candidate with its exact version, size, hash and
whether this app can use it, instead of quietly picking "latest".

Every request goes through `_open`, which owns the network rules:

* HTTPS only, for the first URL and for every redirect hop;
* the destination host is resolved and every address must be globally
  routable, so loopback, private, link-local, carrier-grade NAT and cloud
  metadata endpoints (169.254.169.254) are refused, as are metadata host names;
* redirects are followed by hand, at most five, each re-checked;
* a credential is sent only to its own provider host (the Hugging Face token
  to huggingface.co, the Civitai token to civitai.com), never to a CDN a
  redirect points at;
* bodies are bounded, and every surfaced message has tokens redacted and URL
  query strings (signed CDN links) removed.

This is the model-provider path only. The image-asset import never fetches a
URL, and nothing here is reachable from it.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from .. import log
from ..config import settings
from ..problems import RequestError

logger = log.get("providers")

HF_HOST = "huggingface.co"
CIVITAI_HOST = "civitai.com"
MAX_JSON_BYTES = 8 << 20
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(30.0, connect=15.0)
CHUNK = 1 << 20
_METADATA_HOSTS = frozenset({"metadata", "metadata.google.internal", "instance-data",
                             "metadata.azure.internal"})
# Tests install an httpx.MockTransport here; production uses the default.
transport: httpx.BaseTransport | None = None

PICKLE_SUFFIXES = frozenset({".ckpt", ".pt", ".pth", ".bin", ".pkl", ".pickle"})
_REPO = r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
_HF_URL = re.compile(
    rf"^https://(?:www\.)?huggingface\.co/(?P<repo>{_REPO})"
    r"(?:/(?P<mode>tree|blob|resolve)/(?P<rev>[A-Za-z0-9._-]{1,96})(?:/(?P<path>[^?#]+))?)?/?"
    r"(?:[?#].*)?$")
_HF_ID = re.compile(rf"^(?:hf:)?(?P<repo>{_REPO})(?:@(?P<rev>[A-Za-z0-9._-]{{1,96}}))?$")
_CIVITAI_MODEL = re.compile(
    r"^https://(?:www\.)?civitai\.com/models/(?P<model>\d{1,12})(?:/[^?#]*)?"
    r"(?:\?(?P<q>[^#]*))?(?:#.*)?$")
_CIVITAI_DOWNLOAD = re.compile(r"^https://(?:www\.)?civitai\.com/api/download/models/(?P<version>\d{1,12})(?:\?.*)?$")
_CIVITAI_ID = re.compile(r"^civitai:model/(?P<model>\d{1,12})"
                         r"(?:/version/(?P<version>\d{1,12})(?:/file/(?P<file>\d{1,12}))?)?$")


def _refuse(status: int, detail: str, code: str, field: str | None = None, **extra: Any) -> RequestError:
    return RequestError(status, redact(detail), code=code, field=field, **extra)


def redact(text: str) -> str:
    """No token, and no signed query string, in anything a person or log sees."""
    out = str(text)
    for secret in (settings.hf_token, settings.civitai_api_token):
        if secret:
            out = out.replace(secret, "***")
    return re.sub(r"(https?://[^\s?\"']+)\?[^\s\"']*", r"\1?…", out)


class _RedactRequests(logging.Filter):
    """httpx logs every request URL at INFO, signed CDN query strings
    included. Rewrite those records through `redact` before any handler sees
    them; nothing is dropped."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.msg = redact(record.getMessage())
            record.args = ()
        else:
            record.msg = redact(str(record.msg))
        return True


for _name in ("httpx", "httpcore"):
    if not any(isinstance(f, _RedactRequests) for f in logging.getLogger(_name).filters):
        logging.getLogger(_name).addFilter(_RedactRequests())


def public_url(url: str) -> str:
    """A URL as it may be stored: scheme, host and path only."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _obj(value: Any) -> dict[str, Any]:
    """A provider field that should be an object, or an empty one."""
    return value if isinstance(value, dict) else {}


# ---- destinations ----------------------------------------------------------------------
def resolve_addresses(host: str) -> list[str]:
    """Every address `host` resolves to. Replaced in tests."""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def check_destination(url: str) -> None:
    """Refuse a URL that is not HTTPS to a public address."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise _refuse(400, f"only https downloads are allowed, not {parts.scheme or 'no scheme'}",
                      "insecure_url", "link")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or host in _METADATA_HOSTS or host.endswith(".internal") or host == "localhost":
        raise _refuse(400, f"refusing to fetch from {host or 'an empty host'}",
                      "unsafe_destination", "link")
    try:
        addresses = [host] if _is_ip(host) else resolve_addresses(host)
    except OSError as error:
        raise _refuse(502, f"could not resolve {host}: {error}", "provider_unreachable",
                      "link", retryable=True) from None
    if not addresses:
        raise _refuse(502, f"{host} has no address", "provider_unreachable", "link")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if not ip.is_global or (mapped is not None and not mapped.is_global):
            raise _refuse(400, f"refusing to fetch from {host}: it resolves to a non-public "
                               f"address ({address})", "unsafe_destination", "link")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _auth(url: str) -> dict[str, str]:
    host = (urlsplit(url).hostname or "").lower()
    if host == HF_HOST and settings.hf_token:
        return {"Authorization": f"Bearer {settings.hf_token}"}
    if host == CIVITAI_HOST and settings.civitai_api_token:
        return {"Authorization": f"Bearer {settings.civitai_api_token}"}
    return {}


@contextmanager
def _open(url: str, *, headers: dict[str, str] | None = None) -> Iterator[httpx.Response]:
    """A streamed response for `url`, after checking every hop."""
    current = url
    with httpx.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            check_destination(current)
            request_headers = {"User-Agent": "wizards-brush-import/1",
                               **(headers or {}), **_auth(current)}
            try:
                with client.stream("GET", current, headers=request_headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise _refuse(502, "the provider redirected without a location",
                                          "provider_error", "link")
                        current = urljoin(current, location)
                        continue
                    yield response
                    return
            except httpx.HTTPError as error:
                raise _refuse(502, f"the provider did not answer: {error}",
                              "provider_unreachable", "link", retryable=True) from None
    raise _refuse(502, "too many redirects", "provider_error", "link")


def get_json(url: str) -> Any:
    import json

    with _open(url, headers={"Accept": "application/json"}) as response:
        if response.status_code in {401, 403}:
            raise _refuse(403, "the provider requires access for this model: accept its terms "
                               "on the provider's page and set the token in .env",
                          "access_required", "link")
        if response.status_code == 404:
            raise _refuse(404, "the provider has no such model, version or file",
                          "not_found", "link")
        if response.status_code >= 400:
            raise _refuse(502, f"the provider answered {response.status_code}",
                          "provider_error", "link", retryable=response.status_code >= 500)
        declared = int(response.headers.get("content-length") or 0)
        if declared > MAX_JSON_BYTES:
            raise _refuse(502, "the provider's answer is too large", "provider_error", "link")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > MAX_JSON_BYTES:
                raise _refuse(502, "the provider's answer is too large", "provider_error", "link")
    try:
        return json.loads(bytes(body))
    except ValueError:
        raise _refuse(502, "the provider's answer is not JSON", "provider_error", "link") from None


@dataclass
class Fetched:
    bytes_written: int
    resumed: bool


def download(url: str, part, *, offset: int, limit: int, deadline: float,
             cancel: Callable[[], bool], progress: Callable[[int], None]) -> Fetched:
    """Stream `url` into `part`, resuming at `offset` when the server agrees.

    A 206 whose range starts at `offset` appends; anything else that succeeds
    starts the file over. More than `limit` bytes, or running past
    `deadline`, is refused rather than written.
    """
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with _open(url, headers=headers) as response:
        if response.status_code in {401, 403}:
            raise _refuse(403, "the provider requires access for this file", "access_required",
                          "link")
        if response.status_code == 416 and offset:
            return Fetched(offset, True)          # nothing left to send: the part is whole
        if response.status_code >= 400:
            raise _refuse(502, f"the download answered {response.status_code}",
                          "provider_error", "link", retryable=response.status_code >= 500)
        resumed = False
        start = 0
        if response.status_code == 206 and offset:
            match = re.match(r"bytes (\d+)-", response.headers.get("content-range", ""))
            if match and int(match.group(1)) == offset:
                resumed, start = True, offset
        declared = int(response.headers.get("content-length") or 0)
        if declared and start + declared > limit:
            raise _refuse(400, "the file is larger than allowed", "too_large", "selection")
        written = start
        mode = "ab" if resumed else "wb"
        with open(part, mode) as handle:
            for chunk in response.iter_bytes(CHUNK):
                if cancel():
                    raise _Canceled()
                if time.monotonic() > deadline:
                    raise _refuse(504, "the download took too long", "download_timeout",
                                  "link", retryable=True)
                written += len(chunk)
                if written > limit:
                    raise _refuse(400, "the file is larger than allowed", "too_large",
                                  "selection")
                handle.write(chunk)
                progress(written)
    return Fetched(written, resumed)


class _Canceled(Exception):
    pass


Canceled = _Canceled


# ---- links -----------------------------------------------------------------------------
@dataclass
class Source:
    provider: str                     # "hf" | "civitai"
    repo: str = ""
    revision: str = ""
    path: str = ""
    model_id: int | None = None
    version_id: int | None = None
    file_id: int | None = None
    query: dict[str, list[str]] = field(default_factory=dict)


def parse(link: str) -> Source:
    text = str(link or "").strip()
    if match := _HF_URL.match(text):
        path = (match.group("path") or "").strip("/")
        _check_path(path)
        return Source("hf", repo=match.group("repo"), revision=match.group("rev") or "",
                      path=path)
    if match := _HF_ID.match(text):
        return Source("hf", repo=match.group("repo"), revision=match.group("rev") or "")
    if match := _CIVITAI_MODEL.match(text):
        query = parse_qs(match.group("q") or "")
        version = query.get("modelVersionId", [None])[0]
        return Source("civitai", model_id=int(match.group("model")),
                      version_id=int(version) if version and version.isdigit() else None)
    if match := _CIVITAI_DOWNLOAD.match(text):
        return Source("civitai", version_id=int(match.group("version")))
    if match := _CIVITAI_ID.match(text):
        return Source("civitai", model_id=int(match.group("model")),
                      version_id=int(match.group("version")) if match.group("version") else None,
                      file_id=int(match.group("file")) if match.group("file") else None)
    if text.startswith("http://"):
        raise _refuse(400, "only https links are accepted", "insecure_url", "link")
    raise _refuse(400, "not a Hugging Face or Civitai model link", "unsupported_link", "link",
                  suggestions=["Paste a huggingface.co/<org>/<repo> or civitai.com/models/<id> link."])


def _check_path(path: str) -> None:
    if not path:
        return
    parts = PurePosixPath(path).parts
    if path.startswith("/") or any(p in {"..", "."} for p in parts):
        raise _refuse(400, "the link's file path is not allowed", "invalid_request", "link")


# ---- Hugging Face ----------------------------------------------------------------------
def _license_hf(info: dict[str, Any]) -> dict[str, Any]:
    card = _obj(info.get("cardData"))
    value = str(card.get("license") or "").strip()
    if not value:
        tags = [t for t in info.get("tags") or [] if isinstance(t, str) and t.startswith("license:")]
        value = tags[0].removeprefix("license:") if tags else ""
    if value == "other":
        value = str(card.get("license_name") or "other")
    out: dict[str, Any] = {"id": value or "unknown", "source": "hugging face model card" if value
                           else "none", "reviewed": False}
    if card.get("license_link"):
        out["url"] = str(card["license_link"])[:300]
    return out


def _hf_access(info: dict[str, Any]) -> str:
    gated = info.get("gated")
    if info.get("private"):
        return "private"
    return "gated" if gated not in (None, False, "false") else "public"


def inspect_hf(source: Source) -> dict[str, Any]:
    from ..modelprobe import family_of_model

    revision = source.revision or "main"
    info = get_json(f"https://{HF_HOST}/api/models/{source.repo}/revision/{revision}?blobs=true")
    if not isinstance(info, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(info.get("sha") or "")):
        raise _refuse(502, "the provider did not report an exact commit", "provider_error", "link")
    commit = str(info["sha"])
    siblings = [s for s in info.get("siblings") or [] if isinstance(s, dict) and s.get("rfilename")]
    files = []
    for sibling in siblings:
        name = str(sibling["rfilename"])
        lfs = _obj(sibling.get("lfs"))
        files.append({"path": name, "size": int(lfs.get("size") or sibling.get("size") or 0),
                      "sha256": str(lfs.get("sha256") or "").lower()})
    names = {f["path"] for f in files}
    candidates: list[dict[str, Any]] = []
    if "model_index.json" in names:
        candidates.append(_diffusers_candidate(files))
    for entry in files:
        path = PurePosixPath(str(entry["path"]))
        if len(path.parts) != 1:
            continue
        suffix = path.suffix.lower()
        if suffix == ".safetensors":
            candidates.append({
                "id": f"file:{entry['path']}", "type": "file", "path": entry["path"],
                "name": entry["path"], "size": entry["size"], "sha256": entry["sha256"],
                "kind": None, "supported": True,
                "reason": "" if entry["sha256"] else "the provider lists no sha256 for it; the "
                                                     "file is hashed after download"})
        elif suffix in PICKLE_SUFFIXES:
            candidates.append({"id": f"file:{entry['path']}", "type": "file",
                               "path": entry["path"], "name": entry["path"],
                               "size": entry["size"], "sha256": entry["sha256"], "kind": None,
                               "supported": False, "reason": "pickle-based formats can run code "
                                                             "when loaded and are not imported"})
    if source.path:
        candidates = [c for c in candidates if c.get("path") == source.path] or [
            {"id": f"file:{source.path}", "type": "file", "path": source.path,
             "name": source.path, "supported": False,
             "reason": "this file is not a top-level .safetensors file of the repository"}]
    return _inspection(
        provider="hf", provider_ref=source.repo, revision=commit,
        url=f"https://{HF_HOST}/{source.repo}", name=source.repo,
        license=_license_hf(info), access=_hf_access(info),
        family=family_of_model(source.repo), candidates=candidates,
        notes=([] if source.revision else ["No revision was named; this is the current commit "
                                           "of the default branch, pinned exactly."]),
    )


def _diffusers_candidate(files: list[dict[str, Any]]) -> dict[str, Any]:
    """A whole Diffusers repository: every safetensors weight and config."""
    weights: dict[str, list[dict[str, Any]]] = {}
    keep: list[dict[str, Any]] = []
    for entry in files:
        path = PurePosixPath(str(entry["path"]))
        suffix = path.suffix.lower()
        folder = str(path.parent) if len(path.parts) > 1 else ""
        if suffix in PICKLE_SUFFIXES:
            weights.setdefault(folder, [])
            continue
        if suffix == ".safetensors":
            weights.setdefault(folder, []).append(entry)
            keep.append(entry)
        elif suffix in {".json", ".txt", ".model", ".tiktoken"} or path.name in {"merges.txt", "vocab.json"}:
            keep.append(entry)
    missing = sorted(folder for folder, entries in weights.items() if not entries and folder)
    reason = ("" if not missing else
              f"components without safetensors weights: {', '.join(missing)} (pickle weights are "
              "not imported)")
    return {"id": "repo", "type": "repo", "kind": "diffusers", "name": "Whole Diffusers repository",
            "size": sum(int(e["size"]) for e in keep), "files": keep,
            "supported": not missing, "reason": reason}


# ---- Civitai ---------------------------------------------------------------------------
_CIVITAI_ADAPTERS = frozenset({"lora", "locon", "dora", "lycoris"})
_FAMILY_RULES = (
    ("flux.2", "flux2"), ("flux.1", "flux"), ("flux", "flux"), ("chroma", "chroma"),
    ("qwen", "qwen"), ("zimage", "zimage"), ("z-image", "zimage"), ("z image", "zimage"),
    ("wan", "wan"), ("hunyuan", "hunyuan_video"), ("ltxv", "ltx"), ("sd 3", "sd3"),
    ("sdxl", "sdxl"), ("pony", "sdxl"), ("illustrious", "sdxl"), ("noobai", "sdxl"),
    ("sd 1", "sd"), ("sd 2", "sd"),
)


def civitai_family(base_model: str) -> str:
    text = str(base_model or "").lower()
    return next((family for needle, family in _FAMILY_RULES if needle in text), "unknown")


def _civitai_license(model: dict[str, Any]) -> dict[str, Any]:
    """Civitai publishes permission flags, not a licence identifier. They are
    recorded as the provider stated them; the licence itself stays unknown."""
    return {
        "id": "unknown", "source": "civitai permissions", "reviewed": False,
        "commercial_use": model.get("allowCommercialUse"),
        "derivatives": model.get("allowDerivatives"),
        "credit_required": (not model.get("allowNoCredit")) if "allowNoCredit" in model else None,
        "different_license": model.get("allowDifferentLicense"),
    }


def inspect_civitai(source: Source) -> dict[str, Any]:
    base = f"https://{CIVITAI_HOST}/api/v1"
    model_id = source.model_id
    if model_id is None and source.version_id is not None:
        version = get_json(f"{base}/model-versions/{source.version_id}")
        model_id = int(version.get("modelId") or 0) if isinstance(version, dict) else 0
        if not model_id:
            raise _refuse(502, "Civitai did not say which model this version belongs to",
                          "provider_error", "link")
    model = get_json(f"{base}/models/{model_id}")
    if not isinstance(model, dict):
        raise _refuse(502, "Civitai's answer is not a model", "provider_error", "link")
    model_type = str(model.get("type") or "")
    kind = ("adapter" if model_type.lower() in _CIVITAI_ADAPTERS
            else "model" if model_type.lower() == "checkpoint" else None)
    candidates = []
    versions = [v for v in model.get("modelVersions") or [] if isinstance(v, dict)]
    if source.version_id is not None:
        versions = [v for v in versions if int(v.get("id") or 0) == source.version_id]
        if not versions:
            raise _refuse(404, f"model {model_id} has no version {source.version_id}",
                          "not_found", "link")
    for version in versions:
        for entry in version.get("files") or []:
            if not isinstance(entry, dict):
                continue
            file_id = int(entry.get("id") or 0)
            if source.file_id is not None and file_id != source.file_id:
                continue
            name = str(entry.get("name") or "")
            meta = _obj(entry.get("metadata"))
            fmt = str(meta.get("format") or "")
            sha = str(_obj(entry.get("hashes")).get("SHA256") or "").lower()
            reason = ""
            if kind is None:
                reason = f"Civitai {model_type or 'resources'} are not imported here"
            elif fmt.lower() == "pickletensor" or PurePosixPath(name).suffix.lower() in PICKLE_SUFFIXES:
                reason = "pickle-based formats can run code when loaded and are not imported"
            elif not name.lower().endswith(".safetensors"):
                reason = "only .safetensors files are imported"
            elif str(entry.get("type") or "Model") not in {"Model", "Pruned Model"}:
                reason = f"a {entry.get('type')} file is not the model itself"
            url = str(entry.get("downloadUrl") or "")
            candidates.append({
                "id": f"version:{version.get('id')}/file:{file_id}", "type": "file",
                "version_id": int(version.get("id") or 0),
                "version_name": str(version.get("name") or ""),
                "base_model": str(version.get("baseModel") or ""),
                "family": civitai_family(str(version.get("baseModel") or "")),
                "file_id": file_id, "name": name,
                "size": int(float(entry.get("sizeKB") or 0) * 1024), "sha256": sha,
                "format": fmt, "primary": bool(entry.get("primary")), "kind": kind,
                # Civitai's own link; its query (type, format) selects the file.
                "download_url": url if url.startswith(f"https://{CIVITAI_HOST}/") else "",
                "supported": not reason and bool(url), "reason": reason or (
                    "" if url else "Civitai lists no download for this file"),
            })
    return _inspection(
        provider="civitai", provider_ref=f"model/{model_id}", revision="",
        url=f"https://{CIVITAI_HOST}/models/{model_id}", name=str(model.get("name") or ""),
        license=_civitai_license(model), access="public", family="",
        candidates=candidates, notes=([f"Model type: {model_type}."] if model_type else []),
        extra={"nsfw": bool(model.get("nsfw")), "model_type": model_type},
    )


def _inspection(*, provider: str, provider_ref: str, revision: str, url: str, name: str,
                license: dict[str, Any], access: str, family: str,
                candidates: list[dict[str, Any]], notes: list[str],
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
    supported = [c for c in candidates if c["supported"]]
    return {
        "provider": provider, "provider_ref": provider_ref, "revision": revision, "url": url,
        "name": name, "license": license, "access": access, "family": family,
        "candidates": candidates,
        "selection_required": len(supported) != 1,
        "default_selection": supported[0]["id"] if len(supported) == 1 else None,
        "license_needs_acknowledgement": license.get("id") in {None, "", "unknown"},
        "notes": notes, **(extra or {}),
    }


def inspect(link: str) -> dict[str, Any]:
    """What a link points at. Reads metadata only; downloads no weights."""
    source = parse(link)
    return inspect_hf(source) if source.provider == "hf" else inspect_civitai(source)
