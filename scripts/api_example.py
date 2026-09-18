"""Drive The Wizard's Brush entirely through its HTTP API.

One file, one dependency (httpx, already required by the app), and the whole
round trip a program needs:

  1. import a local image into the library        POST /api/assets/import
  2. edit it once and wait for the result         POST /api/jobs, GET /api/jobs/{id}/wait
  3. fan it out as a Variant Set and wait         POST /api/variant-sets, GET .../wait
  4. download every output, and the set as a ZIP  GET /api/assets/{id}/file, POST .../export

Usage (with the app running, `make start` or `make dev`):

    python scripts/api_example.py path/to/photo.png --out api-example-output
    python scripts/api_example.py photo.png --url http://127.0.0.1:8000 --token "$API_TOKEN"

The edit runs on the Remote GPU lane (Image Edit), so a connected worker is
needed for the jobs to succeed; everything else runs locally. `run()` takes any
httpx-compatible client, which is how the test suite exercises this file
against the real app in-process.
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


class ApiError(RuntimeError):
    pass


def _check(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text
        raise ApiError(f"{response.request.method} {response.request.url.path} "
                       f"-> {response.status_code}: {detail}")
    return response.json()


def wait_for_job(client: httpx.Client, job_id: int, *, limit: float = 1800.0) -> dict[str, Any]:
    """Block until the job settles. Each request long-polls for up to 30 s."""
    deadline = time.monotonic() + limit
    while True:
        answer = _check(client.get(f"/api/jobs/{job_id}/wait", params={"timeout": 30}))
        if answer["settled"] or time.monotonic() > deadline:
            return answer


def wait_for_set(client: httpx.Client, set_id: int, *, limit: float = 7200.0) -> dict[str, Any]:
    deadline = time.monotonic() + limit
    while True:
        answer = _check(client.get(f"/api/variant-sets/{set_id}/wait",
                                   params={"timeout": 30, "items": True}))
        counts = answer["set"]["counts"]
        print(f"  set {set_id}: {answer['set']['status']} · "
              f"{counts.get('succeeded', 0)}/{counts.get('total', 0)} done")
        if answer["settled"] or time.monotonic() > deadline:
            return answer


def download(client: httpx.Client, asset_id: int, dest: Path) -> Path:
    response = client.get(f"/api/assets/{asset_id}/file")
    if response.status_code != 200:
        raise ApiError(f"download of asset {asset_id} failed: {response.status_code}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    return dest


def run(client: httpx.Client, image: Path, out: Path, *, tag: str | None = None,
        wait_limit: float = 7200.0) -> dict[str, Any]:
    """The whole round trip. Returns what it made, for callers and tests."""
    # Idempotency keys: rerunning after a dropped connection returns the same
    # work instead of queuing it twice. Derived from one tag per run.
    tag = tag or uuid.uuid4().hex[:12]

    kinds = {k["kind"]: k for k in _check(client.get("/api/jobs/kinds"))}
    if not kinds.get("image_edit", {}).get("available"):
        raise ApiError("Image Edit is not available on this install")

    # 1. import
    with image.open("rb") as fh:
        source = _check(client.post("/api/assets/import",
                                    files={"file": (image.name, fh, "image/png")},
                                    data={"tags": "api-example"}))
    print(f"imported {image.name} as asset {source['id']} ({source['width']}x{source['height']})")

    # 2. one edit
    submitted = _check(client.post("/api/jobs", json={
        "kind": "image_edit",
        "params": {"prompt": "Place the subject on a plain light grey studio backdrop"},
        "inputs": {"images": [source["id"]]},
        "request_id": f"api-example-{tag}-edit",
    }))
    print(f"queued job {submitted['job_id']}")
    edit = wait_for_job(client, submitted["job_id"], limit=wait_limit)
    if not edit["settled"]:
        raise ApiError(f"job {submitted['job_id']} did not settle within {wait_limit:.0f} s")
    if edit["job"]["status"] != "done":
        raise ApiError(f"edit {edit['job']['status']}: {edit['job']['error'].splitlines()[:1]}")
    edited = [download(client, a["id"], out / "edit" / a["filename"]) for a in edit["assets"]]
    print(f"edit done: {', '.join(str(p) for p in edited)}")

    # 3. a Variant Set: every colour x angle, cut out and sized, then checked
    recipe = {
        "sources": [edit["assets"][0]["id"]],
        "seed": {"mode": "fixed", "value": 1234},
        "stages": [{
            "name": "Colourways",
            "operation": "image_edit",
            "prompt": "Recolour the subject {{colour}}, seen from the {{angle}}",
            "axes": [{"name": "colour", "values": ["slate blue", "moss green"]},
                     {"name": "angle", "values": ["front", "side"]}],
            "finishing": [{"processor": "resize", "width": 512, "height": 512,
                           "mode": "contain", "background": "#f2f2f2"}],
            "validation": {"format": "PNG", "width": 512, "height": 512},
            "naming": {"template": "{{colour}}/{{angle}}"},
        }],
    }
    preview = _check(client.post("/api/variant-sets/preview", json={"recipe": recipe}))
    print(f"preview: {preview['total']} variants, e.g. {preview['items'][0]['output_name']}")
    made = _check(client.post("/api/variant-sets", json={
        "recipe": recipe, "name": f"API example {tag}",
        "request_id": f"api-example-{tag}-set",
    }))
    settled = wait_for_set(client, made["id"], limit=wait_limit)
    if not settled["settled"]:
        raise ApiError(f"set {made['id']} did not settle within {wait_limit:.0f} s")
    outputs = []
    for item in settled["set"]["items"]:
        if item["state"] == "succeeded" and item["asset"]:
            outputs.append(download(client, item["asset"]["id"], out / "set" / item["output_name"]))
        else:
            print(f"  {item['key']}: {item['state']} {item['state_reason']}")

    # 4. the set as one archive, with its manifest
    archive = client.post(f"/api/variant-sets/{made['id']}/export", json={})
    zip_path = out / f"variant-set-{made['id']}.zip"
    if archive.status_code == 200:
        zip_path.write_bytes(archive.content)
        print(f"exported {zip_path}")
    return {"source": source["id"], "edit_job": submitted["job_id"], "edited": edited,
            "set": made["id"], "status": settled["set"]["status"], "outputs": outputs,
            "archive": zip_path if archive.status_code == 200 else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("image", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="", help="API_TOKEN, when the app sets one")
    parser.add_argument("--out", type=Path, default=Path("api-example-output"))
    args = parser.parse_args(argv)
    headers = {"X-API-Token": args.token} if args.token else {}
    with httpx.Client(base_url=args.url, headers=headers, timeout=120.0) as client:
        try:
            result = run(client, args.image, args.out)
        except (ApiError, httpx.HTTPError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    print(f"set {result['set']} finished {result['status']}: {len(result['outputs'])} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
