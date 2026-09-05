# Model and weight licenses

The Wizard's Brush source code is Apache-2.0. Model weights, adapters, CUDA
components, and services are separate works with their own terms. Installing or
selecting a model does not make that model Apache-2.0 and does not transfer any
rights from its publisher.

This catalogue was checked against the linked publisher pages on 2026-09-05.
Upstream terms and metadata can change. Review the current license before every
new deployment, redistribution, commercial use, or hosted service. This page is
an engineering inventory, not legal advice.

## Image and video models

| Configuration | Default | Upstream license/status | Notes |
| --- | --- | --- | --- |
| `LOCAL_IMAGE_MODEL` - [Z-Image-Turbo](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo) | Enabled | Apache-2.0 | Starter local checkpoint |
| `LOCAL_IMAGE_MODEL_HQ` - [Z-Image](https://huggingface.co/Tongyi-MAI/Z-Image) | Disabled | Apache-2.0 | Optional larger local quality slot |
| `LOCAL_IMAGE_MODEL_KLEIN` - [FLUX.2-klein-4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B) | Disabled | Apache-2.0 | The 4B checkpoint is Apache-2.0; other Klein sizes can use different terms |
| `LOCAL_IMAGE_MODEL_CHROMA` - [Chroma1-HD](https://huggingface.co/lodestones/Chroma1-HD) | Disabled | Apache-2.0 | Optional compatibility slot |
| `LOCAL_IMAGE_MODEL_FLUX` - [FLUX.1-dev](https://huggingface.co/black-forest-labs/FLUX.1-dev) | Disabled | Non-commercial model license | Gated and not an open-source model license |
| `LOCAL_IMAGE_MODEL_SDXL` | Disabled | User-selected | The slot accepts many checkpoints; audit the exact repository |
| `A100_IMAGE_MODEL` - [Qwen-Image-2512](https://huggingface.co/Qwen/Qwen-Image-2512) | Enabled for generated worker | Apache-2.0 | Remote image generation |
| `A100_IMAGE_MODEL_HIDREAM` - [HiDream-O1-Image](https://huggingface.co/HiDream-ai/HiDream-O1-Image) | Enabled for generated worker | MIT | Optional second remote image engine |
| `QWEN_EDIT_MODEL` - [Qwen-Image-Edit-2511](https://huggingface.co/Qwen/Qwen-Image-Edit-2511) | Enabled for generated worker | Apache-2.0 | Remote image editing |
| `VIDEO_MODEL` - [Wan2.2-TI2V-5B-Diffusers](https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B-Diffusers) | Enabled for generated worker | Apache-2.0 | Remote video engine |
| `HUNYUAN_VIDEO_MODEL` - [HunyuanVideo-1.5](https://huggingface.co/tencent/HunyuanVideo-1.5) | Disabled | Tencent Hunyuan Community License | Excludes use in the EU, UK, and South Korea; adds hosted-service, use, disclosure, and scale conditions. Not open source |
| `LTX_VIDEO_MODEL` | Disabled | User-selected | Audit the exact checkpoint and every bundled component |

The disabled state matters. A jurisdiction-agnostic public starter file must not
silently opt a user into geographic, field-of-use, non-commercial, or hosted
service restrictions.

## Supporting weights and adapters

| Component | Default | Upstream license/status |
| --- | --- | --- |
| [Qwen-Image-2512-Lightning](https://huggingface.co/lightx2v/Qwen-Image-2512-Lightning) | Remote speed mode | Apache-2.0 |
| [Qwen-Image-Edit-2511-Lightning](https://huggingface.co/lightx2v/Qwen-Image-Edit-2511-Lightning) | Remote edit speed mode | Apache-2.0 |
| [Florence-2-base-ft](https://huggingface.co/florence-community/Florence-2-base-ft) | Caption enrichment | MIT |
| [Z-Image ControlNet Union](https://huggingface.co/alibaba-pai/Z-Image-Turbo-Fun-Controlnet-Union-2.1) | Disabled | Apache-2.0 |
| [lllyasviel/Annotators](https://huggingface.co/lllyasviel/Annotators) | Disabled with ControlNet | Upstream page declares `other` and has no model-card license text; verify the individual annotator artifacts |
| [CLIP ViT-B/32](https://huggingface.co/openai/clip-vit-base-patch32) | Semantic embeddings disabled | Upstream model page has no license tag; verify before enabling or redistributing |
| Curated Z-Image starter LoRAs | Installed by `make models` | Apache-2.0 as declared by each linked model page and recorded in its local JSON sidecar |

User-supplied checkpoints and LoRAs are never covered by this inventory. The app
catalogues and loads LoRAs only as `.safetensors`. PyTorch `.pt` and `.bin` files
use pickle and can execute publisher-controlled Python, so any legacy conversion
belongs in an isolated, trusted environment outside this application.

The public defaults above are fetched at the reviewed Hugging Face commit IDs in
`backend/app/model_sources.py`; changing one is a source-reviewed update. Custom
model slots intentionally follow the repository ID supplied by the operator and
therefore inherit that repository owner's update policy.

## Runtime dependencies

The Python and frontend lockfiles are the exact dependency inventory. The CUDA
PyTorch build pulls NVIDIA runtime packages under NVIDIA terms; the required
NVIDIA driver is also not Apache-2.0. The application source remains FOSS, but a
working local CUDA deployment includes these separately licensed proprietary
components.

The generated frontend bundles Instrument Sans and Newsreader under OFL-1.1.
Their required notices are in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)
and are emitted into the frontend build as `/third-party-notices.txt`.
