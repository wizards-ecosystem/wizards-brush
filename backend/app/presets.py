"""Prompt presets, negative-prompt library, and tuning defaults.

The Wan negative prompts and the tuning guidance are ported from
videogenerator.ipynb so the proven settings carry over to the app.
"""
from __future__ import annotations

# Official Wan negative prompt (Chinese) — noticeably improves video quality.
WAN_NEGATIVE_ZH = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
    "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，"
    "画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，"
    "静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)
WAN_NEGATIVE_EN = (
    "overexposed, oversaturated, blurry, low quality, worst quality, deformed, "
    "bad anatomy, bad hands, extra fingers, bad face, watermark, text, static"
)
# Face/anatomy-focused negative — helps the I2V morphing-hands problem.
# Short and targeted on purpose. The long "lowres, bad anatomy, extra fingers,
# jpeg artifacts, watermark, signature, worst quality…" lists are an SD1.5/SDXL
# habit: those models were trained on alt-text where such tokens were genuinely
# predictive. Z-Image and Qwen-Image train on descriptive natural-language
# captions, where a 30-token negative mostly just spends context. Anatomy terms
# still earn their place on img2img/inpaint, where hands and faces are the
# failure mode people actually hit.
FACE_NEGATIVE = "deformed hands, extra fingers, fused fingers, distorted face, asymmetric eyes"
IMAGE_NEGATIVE = "watermark, text overlay, jpeg artifacts"

NEGATIVE_PRESETS = [
    {"id": "wan_zh", "label": "Wan official (Chinese) — best for video", "text": WAN_NEGATIVE_ZH},
    {"id": "wan_en", "label": "Wan general (English)", "text": WAN_NEGATIVE_EN},
    {"id": "face", "label": "Face & hands focus", "text": FACE_NEGATIVE},
    {"id": "image", "label": "Image quality default", "text": IMAGE_NEGATIVE},
    {"id": "none", "label": "None", "text": ""},
]

PROMPT_PRESETS = [
    # These describe an image-making task and composition, while Styles below
    # supply the medium. That separation makes combinations useful instead of
    # producing "photograph, oil painting, anime" prompt collisions. Natural
    # sentences also suit the caption-trained DiT models better than SD-era
    # quality incantations.
    {"id": "photoreal", "category": "People", "label": "Environmental portrait",
     "text": "an environmental portrait that shows the subject in a meaningful setting, "
             "natural expression, clear separation from the background"},
    {"id": "studio", "category": "People", "label": "Editorial portrait",
     "text": "a waist-up editorial portrait with deliberate wardrobe and styling, "
             "a relaxed pose, soft directional key light and subtle fill"},
    {"id": "character", "category": "People", "label": "Character design",
     "text": "a full-body character design with a readable silhouette, distinctive clothing "
             "and props, neutral background, front three-quarter view"},
    {"id": "film", "category": "People", "label": "Candid moment",
     "text": "a candid unposed moment captured at eye level, natural gesture, believable "
             "surroundings and available light"},
    {"id": "landscape", "category": "Places", "label": "Layered landscape",
     "text": "a wide landscape with a strong foreground anchor, readable middle distance, "
             "atmospheric depth and a clear horizon"},
    {"id": "interior", "category": "Places", "label": "Interior / architecture",
     "text": "an architectural interior photographed from a human viewpoint, straight "
             "vertical lines, balanced natural and practical light, materials rendered accurately"},
    {"id": "product", "category": "Objects", "label": "Product campaign",
     "text": "a commercial product hero shot with a deliberate surface and backdrop, controlled "
             "reflections, soft contact shadow, the product shape completely readable"},
    {"id": "food", "category": "Objects", "label": "Food editorial",
     "text": "an appetizing editorial food composition with intentional plating, tactile "
             "ingredients, soft side light and a restrained supporting scene"},
    {"id": "storybook", "category": "Narrative", "label": "Story illustration",
     "text": "a narrative illustration showing one clear story beat, expressive characters, "
             "foreground and background details that support the action"},
    {"id": "action", "category": "Narrative", "label": "Decisive action",
     "text": "the decisive instant of an action, a clear subject and direction of movement, "
             "dynamic staging without cropping important limbs or props"},
    {"id": "text_sign", "category": "Design", "label": "Poster / exact text",
     "text": "a clean graphic poster with the requested words shown once as large, correctly "
             "spelled lettering, strong hierarchy and generous negative space"},
    {"id": "icon", "category": "Design", "label": "Icon / emblem",
     "text": "a single bold emblem centered on a plain contrasting background, simple "
             "recognizable silhouette, no extra lettering or decorative clutter"},
    {"id": "gentle_motion", "category": "Motion", "label": "Subtle motion",
     "text": "one continuous shot with subtle natural movement, stable subject identity, "
             "consistent lighting and no abrupt pose changes"},
    {"id": "camera_move", "category": "Motion", "label": "Cinematic reveal",
     "text": "one continuous cinematic shot, a slow controlled camera reveal, coherent "
             "background parallax and a clear final composition"},
]

# Model-aware workflow bundles. Only controls named in `values` are changed;
# `requires` prevents an image batch preset appearing on a generator that has no
# batch control. They deliberately leave prompt, model, aspect and seed alone.
SETTING_PRESETS = [
    {"id": "explore", "label": "Explore ×4", "requires": ["quality", "batch"],
     "description": "Four fast seed variations for finding a composition.",
     "values": {"quality": "Draft", "batch": 4, "seed_mode": "increment", "finish": "none"}},
    {"id": "compare", "label": "Compare ×2", "requires": ["quality", "batch"],
     "description": "Two standard-quality candidates with consecutive seeds.",
     "values": {"quality": "Standard", "batch": 2, "seed_mode": "increment", "finish": "none"}},
    {"id": "final", "label": "Final render", "requires": ["quality", "batch"],
     "description": "One model-aware high-quality render without automatic post-processing.",
     "values": {"quality": "High", "batch": 1, "finish": "none"}},
    {"id": "motion_draft", "label": "Motion draft", "requires": ["num_frames", "resolution"],
     "description": "A short 480p clip for checking action and camera movement.",
     "values": {"quality": "Draft", "num_frames": 25, "resolution": "480p",
                "post_interpolate": False}},
    {"id": "motion_final", "label": "Motion final", "requires": ["num_frames", "resolution"],
     "description": "A balanced 720p final clip at the normal shot length.",
     "values": {"quality": "High", "num_frames": 49, "resolution": "720p"}},
]

# Categorized style modifiers — multi-select chips appended to the prompt.
#
# Written as descriptive phrases rather than the old booster-token style
# ("8k, ultra detailed, masterpiece, best quality, octane render"). Those tokens
# are vestigial here: they worked on SD-era models trained on scraped alt-text
# where "masterpiece" correlated with quality. The DiT models this app runs are
# trained on natural-language captions and respond to descriptions of what the
# image should look like, not to quality incantations.
STYLE_PROFILES = [
    {"category": "Photographic", "items": [
        {"id": "photoreal", "label": "Photorealistic",
         "text": "a photograph, natural lighting, realistic skin and material texture"},
        {"id": "portrait85", "label": "85mm portrait",
         "text": "shot on an 85mm lens at f/1.8, subject sharp against a softly blurred background"},
        {"id": "documentary", "label": "Documentary",
         "text": "observational documentary photography, available light, an unposed authentic moment"},
        {"id": "fashion", "label": "Fashion editorial",
         "text": "a fashion editorial photograph, deliberate styling and pose, controlled studio colour"},
        {"id": "architecture", "label": "Architecture",
         "text": "architectural photography, straight verticals, precise geometry, natural material colour"},
        {"id": "food_photo", "label": "Food photography",
         "text": "editorial food photography, tactile ingredients, soft side light, "
                 "appetizing natural colour"},
        {"id": "macro", "label": "Macro",
         "text": "extreme close-up macro photograph, fine surface texture, very shallow focus"},
        {"id": "aerial", "label": "Aerial",
         "text": "aerial drone photograph looking down from high above, wide landscape below"},
    ]},
    {"category": "Cinematic", "items": [
        {"id": "cinematic", "label": "Cinematic",
         "text": "a film still, cinematic lighting, wide anamorphic framing, muted colour grade"},
        {"id": "moody", "label": "Moody / noir",
         "text": "low-key lighting, deep shadows, a single hard light source, film-noir mood"},
        {"id": "epic", "label": "Epic",
         "text": "a vast dramatic scene, the subject small against an immense background, "
                 "shafts of light through atmosphere"},
        {"id": "scifi", "label": "Science fiction",
         "text": "grounded science-fiction production design, functional technology, cinematic scale"},
        {"id": "fantasy", "label": "Fantasy",
         "text": "mythic fantasy production design, tactile handmade detail, atmospheric cinematic light"},
    ]},
    {"category": "Paint & Drawing", "items": [
        {"id": "oil", "label": "Oil painting",
         "text": "an oil painting on canvas, visible brush strokes, layered colour and impasto accents"},
        {"id": "watercolor", "label": "Watercolour",
         "text": "a watercolour painting, translucent washes, controlled blooms and visible paper grain"},
        {"id": "gouache", "label": "Gouache",
         "text": "a gouache painting, opaque matte colour, simplified shapes and confident brush edges"},
        {"id": "inkwash", "label": "Ink wash",
         "text": "an expressive ink-wash painting on textured paper, varied brush pressure and open space"},
        {"id": "charcoal", "label": "Charcoal",
         "text": "a charcoal drawing, broad gestural marks, smudged midtones and sharp "
                 "compressed-black accents"},
        {"id": "pastel", "label": "Soft pastel",
         "text": "a soft-pastel drawing on toned paper, layered powdery colour and visible "
                 "hand-worked marks"},
    ]},
    {"category": "Illustration & Print", "items": [
        {"id": "anime", "label": "Anime",
         "text": "anime illustration, clean confident line art, flat cel shading, vivid colour"},
        {"id": "concept", "label": "Concept art",
         "text": "production concept art, painterly rendering, strong silhouette, "
                 "clear read at a glance"},
        {"id": "comic", "label": "Comic",
         "text": "a comic book panel, bold ink outlines, halftone dot shading, flat colour"},
        {"id": "storybook", "label": "Storybook",
         "text": "a storybook illustration, expressive shapes, warm narrative detail and "
                 "hand-painted texture"},
        {"id": "editorial_vector", "label": "Editorial vector",
         "text": "a clean editorial vector illustration, geometric shapes, limited palette and crisp edges"},
        {"id": "linocut", "label": "Linocut",
         "text": "a hand-carved linocut print, bold black shapes, rough cut marks and limited spot colour"},
        {"id": "collage", "label": "Paper collage",
         "text": "a layered paper collage, cut edges, printed textures, shadows between physical pieces"},
    ]},
    {"category": "3D & Render", "items": [
        {"id": "octane", "label": "3D render",
         "text": "a 3D render with physically based materials, global illumination, "
                 "accurate reflections"},
        {"id": "pixar", "label": "Stylized 3D",
         "text": "a stylized 3D character with soft rounded forms and subsurface scattering in the skin"},
        {"id": "clay", "label": "Clay model",
         "text": "a handmade clay model, fingerprints and sculpting marks, soft studio light"},
        {"id": "isometric", "label": "Isometric",
         "text": "an isometric 3D scene viewed from a fixed 45-degree angle, clean studio lighting"},
        {"id": "miniature", "label": "Miniature",
         "text": "a handcrafted scale miniature, tilt-shift depth of field, tiny practical materials"},
    ]},
    {"category": "Vintage & Film", "items": [
        {"id": "analog", "label": "Analog film",
         "text": "shot on 35mm Kodak Portra, warm tones, visible film grain, slightly soft"},
        {"id": "polaroid", "label": "Polaroid",
         "text": "an instant Polaroid photograph, washed-out colour, soft focus, light leaks at the edge"},
        {"id": "monochrome", "label": "Black & white",
         "text": "black-and-white film photography, rich midtone separation, restrained grain"},
        {"id": "retro80", "label": "80s retro",
         "text": "1980s aesthetic, neon signage against night, saturated magenta and cyan"},
    ]},
    {"category": "Lighting", "items": [
        {"id": "golden", "label": "Golden hour",
         "text": "late golden-hour sunlight, long shadows, warm rim light on the subject"},
        {"id": "studio", "label": "Studio",
         "text": "studio lighting with a large soft key light and gentle fill, seamless backdrop"},
        {"id": "overcast", "label": "Soft overcast",
         "text": "soft overcast daylight, gentle shadow transitions and accurate muted colour"},
        {"id": "chiaroscuro", "label": "Chiaroscuro",
         "text": "dramatic chiaroscuro, a narrow directional light carving the subject from deep shadow"},
        {"id": "neon", "label": "Neon",
         "text": "lit by coloured neon, saturated pools of light and deep surrounding darkness"},
        {"id": "rim", "label": "Rim light",
         "text": "backlit so a bright rim traces the subject's edge against a dark background"},
    ]},
    {"category": "Composition", "items": [
        {"id": "closeup", "label": "Close-up",
         "text": "a tight close-up filling the frame with the subject"},
        {"id": "symmetry", "label": "Centered symmetry",
         "text": "precise centered composition with strong bilateral symmetry and balanced visual weight"},
        {"id": "thirds", "label": "Rule of thirds",
         "text": "the main subject placed on a rule-of-thirds intersection with intentional lead room"},
        {"id": "overhead", "label": "Overhead flat lay",
         "text": "directly overhead flat-lay composition, objects arranged with clean spacing and rhythm"},
        {"id": "establishing", "label": "Wide establishing",
         "text": "a wide establishing composition with clear foreground, middle ground and background"},
        {"id": "negative_space", "label": "Negative space",
         "text": "a restrained composition with generous intentional negative space around the subject"},
    ]},
]

# Camera moves for video, appended to the prompt.
#
# Wan and LTX take no camera parameter — motion is expressed in the text, and
# these models were captioned with cinematography vocabulary, so the phrasing
# matters more than the intent. Curated here rather than left to the user so the
# wording is the kind the models actually saw in training.
CAMERA_MOVES = {
    "none": "",
    "static": "locked-off static camera, no camera movement",
    "push_in": "slow steady dolly push in toward the subject",
    "pull_out": "slow steady dolly pull back, revealing more of the scene",
    "pan_left": "smooth camera pan to the left",
    "pan_right": "smooth camera pan to the right",
    "tilt_up": "smooth camera tilt upward",
    "tilt_down": "smooth camera tilt downward",
    "orbit": "camera orbits slowly around the subject",
    "crane_up": "crane shot rising steadily upward",
    "handheld": "handheld camera with subtle natural shake",
    "drone": "aerial drone shot flying slowly forward",
}

CAMERA_OPTIONS = list(CAMERA_MOVES)


def apply_camera(prompt: str, move: str) -> str:
    """Append a camera move to a video prompt. Unknown or 'none' is a no-op."""
    text = CAMERA_MOVES.get(move or "none", "")
    if not text:
        return prompt
    base = (prompt or "").rstrip().rstrip(",")
    return f"{base}, {text}" if base else text


# Quality tier -> inference steps, per device group ("local"/"a100"/"video").
QUALITY_STEPS = {
    "local": {"Draft": 6, "Standard": 9, "High": 15},
    "local_hq": {"Draft": 15, "Standard": 28, "High": 40},  # Z-Image base (CFG model)
    # Distilled FLUX.2-klein-4B uses four steps and CFG 1 in the official
    # consumer-GPU recipe. More steps are not a quality tier for this checkpoint.
    "local_klein": {"Draft": 4, "Standard": 4, "High": 4},
    # Chroma's model card runs 40 steps at CFG 3. FLUX.1-dev's reference
    # quality setting is 50 steps at CFG 3.5; Standard stays at 28 for a
    # practical interactive run, while High is the documented quality target.
    "local_chroma": {"Draft": 20, "Standard": 30, "High": 40},
    "local_flux": {"Draft": 16, "Standard": 28, "High": 50},
    # Qwen-Image-2512's own example is 50 steps with true CFG 4.0. The A100
    # can afford that final-quality pass, so High should mean the documented
    # recipe rather than an arbitrary near miss.
    "a100": {"Draft": 20, "Standard": 30, "High": 50},
    "a100_hidream": {"Draft": 28, "Standard": 40, "High": 50},
    # SDXL community checkpoints are typically published with 30 steps at CFG
    # 2.5-4.5. SDXL flattens out well before the generic a100 row's 45, so High
    # buys a little headroom, not 50%.
    "a100_sdxl": {"Draft": 20, "Standard": 30, "High": 36},
    # Same architecture, same answer, wherever it runs.
    "local_sdxl": {"Draft": 20, "Standard": 30, "High": 36},
    # FLUX.2-klein is a flow-matching DiT in the Flux lineage: 28 is the
    # reference, and it keeps improving to ~40 unlike the distilled models.
    "a100_flux2": {"Draft": 16, "Standard": 28, "High": 40},
    # The locally configured custom slot follows its model's documented recipe.
    "a100_flux": {"Draft": 16, "Standard": 28, "High": 50},
    "video": {"Draft": 25, "Standard": 40, "High": 60},
}
QUALITY_TIERS = ["Draft", "Standard", "High", "Custom"]


def quality_steps(group: str, tier: str, fallback: int) -> int:
    """Map a quality tier to a step count for a device group; fallback on Custom/unknown."""
    return QUALITY_STEPS.get(group, {}).get(tier, fallback)


# Aspect ratios offered in the visual picker (must match base.ASPECTS keys), + Custom.
ASPECT_OPTIONS = ["1:1", "3:4", "4:3", "2:3", "3:2", "9:16", "16:9", "Custom"]

# Per-control hints. *_local / *_a100 / *_video suffixes let the UI show
# model-specific guidance via the control's hint_key.
TUNING_HINTS = {
    "num_frames": "Wan needs 4k+1 frames: 25 (~1.2s), 49 (~2.4s), 81 (~4s), 121 (~6s) @ 20fps.",
    "steps": "More steps = more detail, slower. Diminishing returns past the High tier.",
    "steps_local": "Depends on the model: Turbo is distilled (6/9/15, >20 wastes time); "
                   "Z-Image base 15/28/40; Chroma 20/30/40; Flux.1-dev 16/28/50.",
    "steps_a100": "Qwen-Image-2512: 20 draft · 30 standard · 50 documented quality.",
    "steps_video": "Wan video: 25 draft · 40 standard · 60 cleaner. Big quality jump up to ~40.",
    "guidance": "Higher = obeys the prompt harder; too high looks stiff/oversaturated.",
    "guidance_a100_sdxl": "SDXL checkpoints want 2.5-4.5 — this one bakes in a lot of its "
                          "look and burns out above ~5. Keep prompts reasonable too: "
                          "camera bodies, film stocks and lighting do more here than piling "
                          "on quality adjectives.",
    "negative_prompt": "What to steer away from. Note that at CFG 0 (the official Turbo setting) "
                       "the model ignores it entirely — switch to the Quality "
                       "model, for a negative to do anything. Short and specific beats a long list "
                       "on these models.",
    "prompt_syntax": "A1111-compatible removes pasted emphasis marks such as (detail:1.3) "
                     "before T5/Qwen encoding. Literal preserves every bracket and number. "
                     "The choice is stored per job so reruns do not change meaning.",
    "guidance_local": "Z-Image-Turbo is distilled for CFG 0.0 (official 9-step recipe). "
                      "Higher CFG can wash out the image; use Z-Image base for controllable CFG "
                      "and negative prompting.",
    "guidance_a100": "Qwen real CFG (true_cfg): 3.5–5 natural · 6–7 strong prompt/text adherence.",
    "guidance_video": "Wan: 3–4 looser/natural · 5 default · 6–7 obeys harder but stiffer.",
    "sampler": "Which solver walks the denoising path. default/euler is the model's own; "
               "lcm is few-step consistency sampling that suits the distilled Turbo model; "
               "flowmap takes a different trajectory at the same cost. Same seed, different "
               "solver, different image.",
    "seed": "Same inputs + new seed = a different result. Cycle seeds before changing settings.",
    "aspect": "Pick the framing; the quality tier sets the pixel budget. Custom unlocks exact W/H.",
    "aspect_input": "Match input keeps the source framing. Pick another ratio only when you "
                    "deliberately want a center crop.",
    "quality": "Draft = fast preview · Standard = balanced · High = best · Custom = set steps yourself.",
    "guidance_turbo": "Z-Image-Turbo is distilled for CFG 0. Negative prompts are ignored; "
                       "use the Quality model when you need true negative steering.",
    "guidance_zimage": "Z-Image base uses real CFG: 3–5 is the useful range and 4 is the balanced default.",
    "guidance_klein": "FLUX.2-klein-4B is distilled for CFG 1 at four steps. Treat that as "
                       "part of the model recipe.",
    "guidance_sdxl": "Most SDXL checkpoints prefer CFG 2.5–4.5; higher values often burn "
                      "highlights and texture.",
    "guidance_chroma": "Chroma-family checkpoints are generally balanced near CFG 3; "
                        "raise it only for adherence.",
    "guidance_flux": "FLUX development checkpoints are generally balanced near CFG 3.5.",
    "guidance_hidream": "HiDream-O1's published full-model recipe uses CFG 5 and 50 steps.",
    "resolution": "720p for final/faces · 480p for fast iteration.",
    "strength_outpaint": "How far the new area may depart from the source. 0.65 keeps tone "
                         "and lighting continuous; above ~0.85 the model stops respecting the "
                         "existing edges and a visible seam appears.",
    "direction": "Which edges to grow. The original pixels are never scaled or "
                 "cropped — everything new is painted around them.",
    "expand_pct": "How much to add per extended edge, as a percent of the source. "
                  "25-40% at a time beats one big jump; run it again to go further.",
    "strength": "Img2img/inpaint: 0.3 subtle edit · 0.6 balanced · 0.9 mostly new.",
    "inpaint_area": "Masked area spends the full model resolution on your painted region and "
                    "preserves the rest exactly. Whole image gives the model more global context.",
    "mask_grow": "Expand the generation mask slightly so the model can replace boundary pixels, too.",
    "mask_padding": "Unmasked context around the painted area shown to the model. More context helps "
                    "lighting and structure; less puts more resolution into a tiny edit.",
    "mask_blur": "Blend inward at the mask edge. Pixels outside the painted mask remain unchanged.",
    "camera": "How the camera moves. Wan and LTX take no camera parameter — this is"
              "appended to the prompt in the cinematography wording the models were"
              "captioned with.",
    "fps": "Playback rate. 16–24 is natural; pair with frame count for duration.",
    "finish": "What to run on the result after it is generated, on the local GPU. "
              "faces = detail pass + GFPGAN restore · upscale = Real-ESRGAN ×4 "
              "(slow, and quadruples the file) · custom exposes each step separately.",
    "post_scale": "Real-ESRGAN upscale factor applied after generation. Output is "
                  "proportionally capped at a 4096 px long side for safe files.",
    "seed_mode": "Across a batch: increment (varied) · fixed (same seed) · random (independent).",
    "combinatorial": "Queue one job per {a|b|c} combination (capped at 32). "
                     "__wildcards__ remain reproducible seed-keyed picks.",
    "model_variant_colab": "Which configured Remote GPU image model to run (Qwen, HiDream, "
                           "or an optional ALT/custom slot). The A100 holds one "
                           "at a time — switching evicts and reloads, which also costs a "
                           "download on the first use of each model per session.",
    "model_variant": "turbo = Z-Image distilled (8 DiT forwards, CFG 0), fastest · quality = "
                     "Z-Image base (real CFG) · klein = FLUX.2-klein-4B (4 steps, CFG 1) · "
                     "sdxl = your configured SDXL checkpoint "
                     "(no quantization needed, deepest LoRA ecosystem) · chroma = 8.9B "
                     "de-distilled Flux-schnell, Apache-2.0 · flux = FLUX.1-dev 12B "
                     "(gated: accept the licence on huggingface.co and set HF_TOKEN). Only "
                     "one is resident at a time — switching reloads (~1-2 min, longer on "
                     "first use while the weights download).",
    "speed_mode": "Lightning distill LoRA: 4 steps, CFG 1, negative ignored. ~5-10x faster, "
                  "slightly less detail. Needs a Remote GPU worker with the LoRA loaded.",
    "engine": "wan = photoreal humans and motion · hunyuan = physics/dynamics · "
              "ltx = the only open-weights family with synced audio. Switching "
              "engines downloads and loads the other model on the A100 (minutes, "
              "and LTX needs 150-200 GB of free disk).",
    "post_detail": "Detect faces and re-render each at low denoise (ADetailer-style). "
                   "Runs on the local GPU after generation.",
    "loras": "Drop .safetensors into models/loras/ and they appear here. Stack up to 4; "
             "weight 0.6-0.8 is usually where a style LoRA sits without taking over. "
             "Negative weights push away from the trained concept.",
    "control_mode": "canny = edges (no extra deps) · depth / pose need the [control] extra. "
                    "none = you are supplying a ready-made control map. "
                    "Requires LOCAL_OFFLOAD=false.",
    "control_weight": "How hard the control map constrains the result. 0.7-0.8 works best "
                      "with Turbo; 1.0 tends to over-constrain.",
}


def presets_payload() -> dict:
    from . import db  # lazy: avoids import cycle, lets user presets merge in

    try:
        user = [
            {"id": p.id, "type": p.type, "name": p.name, "payload": p.payload, "scope": p.scope}
            for p in db.list_user_presets()
        ]
    except Exception:  # noqa: BLE001 — degrade quietly; the caller must not fail here
        user = []
    return {
        "negative": NEGATIVE_PRESETS,
        "prompt": PROMPT_PRESETS,
        "settings": SETTING_PRESETS,
        "style_profiles": STYLE_PROFILES,  # categorized
        "aspects": ASPECT_OPTIONS,
        "quality_tiers": QUALITY_TIERS,
        "hints": TUNING_HINTS,
        "user": user,
    }
