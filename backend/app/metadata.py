"""Metadata embedded in the files we write.

Two audiences, two formats, both small.

**Provenance.** IPTC's `DigitalSourceType` is the standard field for saying an
image was machine-generated. It has two values that matter here, and the
distinction is easy to get wrong:

    trainedAlgorithmicMedia              — the whole image was generated
    compositeWithTrainedAlgorithmicMedia — generated content combined with real
                                           pixels (inpaint, outpaint, img2img)

An outpaint keeps the original photograph in the middle. Claiming the whole file
is synthetic would be false, and the composite value exists precisely for that
case.

**Interoperability.** A1111-style PNG text chunks are what the rest of the
ecosystem reads. Dropping an image into any other tool and having it recover the
prompt and seed costs us one text chunk.

Neither of these replaces the database. They travel with the file once it leaves
the gallery, which the database cannot.
"""
from __future__ import annotations

import io
import json
import re
from typing import Any

from PIL import Image

IPTC_NS = "http://iptc.org/std/DigitalSourceType/2021-04-27/"

# Job kinds whose output contains real pixels alongside generated ones.
_COMPOSITE_KINDS = frozenset({
    "img2img", "inpaint", "outpaint", "image_edit", "upscale", "face_restore",
    "detail", "interpolate", "extend_video", "i2v",
    # File writers pass the persisted generator identifier, not the route's job
    # kind. Keep both vocabularies explicit so real source pixels are never
    # mislabeled as a wholly synthetic image in the XMP packet.
    "local_image:img2img", "local_image:inpaint", "local_image:outpaint",
    "local_flux:img2img", "local_flux:inpaint", "colab_edit",
})

NATIVE_CHUNK = "wizards_brush"
NATIVE_SCHEMA = "wizards-brush/image"
NATIVE_SCHEMA_VERSION = 1

_SAMPLER_TO_A1111 = {
    "default": "Euler",
    "euler": "Euler",
    "lcm": "LCM",
    "flowmap": "Euler",
}
_SAMPLER_FROM_A1111 = {
    "euler": "euler",
    "euler a": "euler",
    "lcm": "lcm",
    "flowmap euler": "flowmap",
}
_LORA_TAG = re.compile(
    r"<lora:(?P<name>[^:<>]+):(?P<weight>[-+]?(?:\d+(?:\.\d*)?|\.\d+))>", re.IGNORECASE)
_SETTING_PAIR = re.compile(
    r'(?:^|,\s*)(?P<key>[A-Za-z0-9 _/.-]+):\s*'
    r'(?P<value>"(?:\\.|[^"\\])*"|[^,]*)'
    r'(?=,\s*[A-Za-z0-9 _/.-]+:\s*|$)'
)


def digital_source_type(kind: str) -> str:
    """The IPTC value for a job of this kind."""
    suffix = ("compositeWithTrainedAlgorithmicMedia" if kind in _COMPOSITE_KINDS
              else "trainedAlgorithmicMedia")
    return IPTC_NS + suffix


def xmp_packet(kind: str) -> str:
    """A minimal XMP packet declaring how this image was made.

    Hand-built rather than via a library: it is one field, the format is fixed,
    and an XMP dependency to write forty bytes of RDF would be a poor trade.
    """
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '   xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/"\n'
        f'   Iptc4xmpExt:DigitalSourceType="{digital_source_type(kind)}"/>\n'
        ' </rdf:RDF>\n'
        '</x:xmpmeta>\n'
        '<?xpacket end="w"?>'
    )


def parameters_text(meta: dict[str, Any]) -> str:
    """An A1111-style `parameters` string, which most other tools can read.

    Format is positional and load-bearing: prompt on the first line, negative on
    the second prefixed with "Negative prompt:", then a single comma-separated
    line of key/value settings. Deviating breaks the parsers this exists for.

    LoRAs are folded into the prompt as `<lora:name:weight>` because that is where
    every reader looks for them.
    """
    prompt = str(meta.get("prompt") or "")
    for lora in meta.get("loras") or []:
        if isinstance(lora, dict) and lora.get("path"):
            name = str(lora["path"]).rsplit("/", 1)[-1].rsplit(".", 1)[0]
            prompt += f" <lora:{name}:{float(lora.get('weight', 1.0)):g}>"

    lines = [prompt.strip()]
    negative = str(meta.get("negative_prompt") or "").strip()
    if negative:
        lines.append(f"Negative prompt: {negative}")

    settings: list[str] = []

    def add(label: str, key: str, fmt: str = "{}") -> None:
        value = meta.get(key)
        if value not in (None, "", []):
            settings.append(f"{label}: {_quote_value(fmt.format(value))}")

    add("Steps", "steps")
    sampler = str(meta.get("sampler") or "")
    if sampler:
        settings.append(f"Sampler: {_quote_value(_SAMPLER_TO_A1111.get(sampler, sampler))}")
        settings.append("Schedule type: Flow Match")
    add("CFG scale", "guidance")
    add("Seed", "seed")
    # A derivative keeps the ancestor's generation recipe here. Its native JSON
    # `width`/`height` and structured `post` history describe the current file.
    recipe_width = meta.get("generation_width") or meta.get("width")
    recipe_height = meta.get("generation_height") or meta.get("height")
    if recipe_width and recipe_height:
        settings.append(f"Size: {recipe_width}x{recipe_height}")
    add("Model", "model")
    add("Model variant", "model_variant")
    add("Denoising strength", "strength")
    settings.append("Generator: The Wizard's Brush")
    from .version import get_version

    settings.append(f"Version: {_quote_value(get_version())}")
    lines.append(", ".join(settings))
    return "\n".join(lines)


def _quote_value(value: str) -> str:
    """Quote a settings value exactly when the comma grammar needs it."""
    return json.dumps(value, ensure_ascii=False) if any(c in value for c in ",:\n") else value


def native_metadata_text(meta: dict[str, Any], kind: str) -> str:
    """Lossless, versioned record for The Wizard's Brush round-trips."""
    from .version import get_version

    return json.dumps({
        "schema": NATIVE_SCHEMA,
        "schema_version": NATIVE_SCHEMA_VERSION,
        "app_version": get_version(),
        "generator": kind,
        "params": meta,
    }, ensure_ascii=False, separators=(",", ":"))


def png_text_chunks(
    meta: dict[str, Any], kind: str, *, include_metadata: bool = True,
    include_provenance: bool = True,
) -> dict[str, str]:
    """Independent reproducibility and provenance chunks for a PNG."""
    chunks: dict[str, str] = {}
    if include_metadata:
        chunks["parameters"] = parameters_text(meta)
        chunks[NATIVE_CHUNK] = native_metadata_text(meta, kind)
    if include_provenance:
        chunks["XML:com.adobe.xmp"] = xmp_packet(kind)
    return chunks


def _decode_exif_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        return ""
    for prefix, encoding in ((b"ASCII\x00\x00\x00", "utf-8"),
                             (b"UNICODE\x00", "utf-16")):
        if value.startswith(prefix):
            value = value[len(prefix):]
            with __import__("contextlib").suppress(UnicodeDecodeError):
                return value.decode(encoding).rstrip("\x00")
    return value.decode("utf-8", errors="replace").rstrip("\x00")


def _unquote_value(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, str) else str(decoded)
        except (ValueError, TypeError):
            return value[1:-1]
    return value


def parse_a1111(text: str) -> tuple[dict[str, Any], list[str]]:
    """Parse a foreign parameters block without evaluating embedded values."""
    text = text[:1_000_000].replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return {}, []
    lines = text.split("\n")
    matches = list(_SETTING_PAIR.finditer(lines[-1])) if lines else []
    settings_line = len(matches) >= 3
    body = lines[:-1] if settings_line else lines
    settings = {_unquote_value(m.group("key")): _unquote_value(m.group("value"))
                for m in matches} if settings_line else {}

    negative_at = next((i for i, line in enumerate(body)
                        if line.startswith("Negative prompt:")), None)
    if negative_at is None:
        prompt, negative = "\n".join(body).strip(), ""
    else:
        prompt = "\n".join(body[:negative_at]).strip()
        negative = "\n".join([
            body[negative_at][len("Negative prompt:"):].lstrip(),
            *body[negative_at + 1:],
        ]).strip()

    params: dict[str, Any] = {"prompt": prompt}
    if negative:
        params["negative_prompt"] = negative
    unresolved: list[str] = []

    def number(label: str, typ: type[int] | type[float], target: str) -> None:
        if label not in settings:
            return
        try:
            params[target] = typ(settings[label])
        except (TypeError, ValueError):
            unresolved.append(f"{label}: {settings[label]}")

    number("Steps", int, "steps")
    if "steps" in params:
        params["quality"] = "Custom"
    number("CFG scale", float, "guidance")
    number("Seed", int, "seed")
    number("Denoising strength", float, "strength")
    sampler = settings.get("Sampler", "").removesuffix(" Karras").strip().lower()
    if sampler:
        if sampler in _SAMPLER_FROM_A1111:
            params["sampler"] = _SAMPLER_FROM_A1111[sampler]
        else:
            unresolved.append(f"Sampler: {settings['Sampler']}")
    size = settings.get("Size", "")
    if match := re.fullmatch(r"(\d{1,5})x(\d{1,5})", size.strip()):
        params.update(aspect="Custom", width=int(match.group(1)), height=int(match.group(2)))
    elif size:
        unresolved.append(f"Size: {size}")
    _resolve_model(settings.get("Model variant") or settings.get("Model"), params, unresolved)
    _merge_prompt_loras(params, unresolved)
    return params, unresolved[:50]


def _resolve_model(value: Any, params: dict[str, Any], unresolved: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    from .generators import variants

    value = value.strip()
    names = {v.name for lane in variants.LANES for v in variants.available(lane)}
    name = value if value in names else next(
        (variants.name_of_repo(value, lane) for lane in variants.LANES
         if variants.name_of_repo(value, lane)), "")
    if name:
        params["model_variant"] = name
    else:
        unresolved.append(f"Model: {value}")


def _lora_catalog_by_stem() -> dict[str, str]:
    from .loras import list_loras

    out: dict[str, str] = {}
    for item in list_loras():
        for value in (item.get("name"), item.get("filename"), item.get("path")):
            if value:
                out.setdefault(str(value).rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold(),
                               str(item["path"]))
    return out


def _resolve_lora_items(raw: Any, unresolved: list[str]) -> list[dict[str, Any]]:
    from .loras import MAX_ACTIVE, WEIGHT_MAX, WEIGHT_MIN

    catalogue = _lora_catalog_by_stem()
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            name, weight = item, 1.0
        elif isinstance(item, dict):
            name, weight = str(item.get("path") or item.get("name") or ""), item.get("weight", 1.0)
        else:
            continue
        stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold()
        path = catalogue.get(stem)
        if not path:
            if name:
                unresolved.append(f"LoRA: {name}")
            continue
        try:
            amount = max(WEIGHT_MIN, min(float(weight), WEIGHT_MAX))
        except (TypeError, ValueError):
            amount = 1.0
        if path not in seen:
            seen.add(path)
            resolved.append({"path": path, "weight": amount})
        if len(resolved) >= MAX_ACTIVE:
            break
    return resolved


def _merge_prompt_loras(params: dict[str, Any], unresolved: list[str]) -> None:
    prompt = str(params.get("prompt") or "")
    tagged = [{"name": match.group("name").strip(), "weight": match.group("weight")}
              for match in _LORA_TAG.finditer(prompt)]
    params["prompt"] = _clean_lora_tags(prompt)
    # Native picker entries win over tags, matching the UI's explicit choice.
    merged = _resolve_lora_items([*(params.get("loras") or []), *tagged], unresolved)
    if merged:
        params["loras"] = merged
    else:
        params.pop("loras", None)


def _clean_lora_tags(prompt: str) -> str:
    clean = _LORA_TAG.sub("", prompt)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r",\s*(?:,\s*)+", ", ", clean)
    return "\n".join(line.strip(" ,") for line in clean.split("\n")).strip()


def read_image_metadata(data: bytes) -> dict[str, Any]:
    """Read our native record or a foreign A1111/EXIF block, prefill-only."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            info = dict(getattr(image, "text", {}) or image.info)
            native = info.get(NATIVE_CHUNK)
            parameters = info.get("parameters")
            if not parameters:
                parameters = _decode_exif_text(image.getexif().get(0x9286))
    except Exception as exc:  # one actionable error at the file boundary
        raise ValueError("The selected file is not a readable image.") from exc

    unresolved: list[str] = []
    if isinstance(native, str) and len(native) <= 1_000_000:
        try:
            doc = json.loads(native)
        except (ValueError, TypeError):
            doc = None
        if isinstance(doc, dict) and doc.get("schema") == NATIVE_SCHEMA:
            raw = doc.get("params")
            params = dict(raw) if isinstance(raw, dict) else {}
            model_identity = params.get("model_variant") or params.get("model")
            safe_names = _prefill_names()
            params = {k: v for k, v in params.items() if k in safe_names}
            if isinstance(params.get("raw_prompt"), str):
                params["prompt"] = params.pop("raw_prompt")
            _resolve_model(model_identity, params, unresolved)
            _merge_prompt_loras(params, unresolved)
            return {"scheme": "wizards-brush", "params": params,
                    "unresolved": unresolved[:50], "app_version": doc.get("app_version")}
    if isinstance(parameters, str) and parameters.strip():
        params, unresolved = parse_a1111(parameters)
        return {"scheme": "a1111", "params": params, "unresolved": unresolved}
    return {"scheme": "none", "params": {}, "unresolved": []}


def _prefill_names() -> set[str]:
    from .generators.registry import registry

    return {str(control["name"]) for spec in registry() for control in spec.get("controls", [])} | {
        "raw_prompt", "style_ids", "loras",
    }
