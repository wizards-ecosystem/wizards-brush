"""Registry growth must not leak unsupported kwargs into model-specific APIs."""
from __future__ import annotations

from backend.app.generators import local_image


def test_pipeline_call_filters_unknown_model_specific_keywords():
    class Pipe:
        def __call__(self, prompt, width=512):
            return None

    got = local_image._supported_call_kwargs(
        Pipe(), {"prompt": "x", "width": 768, "cfg_truncation": 0.8}
    )
    assert got == {"prompt": "x", "width": 768}


def test_pipeline_call_preserves_everything_for_kwargs_aware_pipelines():
    class Pipe:
        def __call__(self, prompt, **kwargs):
            return None

    values = {"prompt": "x", "future_option": True}
    assert local_image._supported_call_kwargs(Pipe(), values) == values


def test_flux_style_pipeline_gets_both_embedded_and_true_cfg_for_a_negative():
    class Pipe:
        def __call__(
            self, prompt, negative_prompt=None, guidance_scale=3.5, true_cfg_scale=1.0,
        ):
            return None

    assert local_image._cfg_call_kwargs(Pipe(), 3.5, "blur") == {
        "guidance_scale": 3.5,
        "true_cfg_scale": 3.5,
    }


def test_conventional_pipeline_is_not_given_true_cfg():
    class Pipe:
        def __call__(self, prompt, negative_prompt=None, guidance_scale=3.5):
            return None

    assert local_image._cfg_call_kwargs(Pipe(), 4.0, "blur") == {"guidance_scale": 4.0}


def test_sdxl_quantizes_its_real_component_names():
    assert local_image._quant_components("stabilityai/stable-diffusion-xl-base-1.0") == [
        "unet", "text_encoder_2"
    ]
    assert local_image._quant_components("Tongyi-MAI/Z-Image-Turbo") == [
        "transformer", "text_encoder"
    ]


def test_sdxl_default_installs_dpmpp_sde_karras(monkeypatch):
    import sys
    from types import SimpleNamespace

    from backend.app.generators import schedulers

    seen: dict[str, object] = {}

    class DPM:
        @classmethod
        def from_config(cls, config, **kwargs):
            seen.update(config=config, **kwargs)
            return "tuned"

    monkeypatch.setitem(sys.modules, "diffusers", SimpleNamespace(DPMSolverMultistepScheduler=DPM))
    pipe = SimpleNamespace(scheduler=SimpleNamespace(config={"beta": 1}))
    effective = schedulers.apply(pipe, "default", model="custom/sdxl-checkpoint")
    assert effective == "dpmpp_2m_sde_karras"
    assert pipe.scheduler == "tuned"
    assert seen == {
        "config": {"beta": 1},
        "algorithm_type": "sde-dpmsolver++",
        "use_karras_sigmas": True,
    }


def test_flow_model_default_scheduler_is_untouched():
    from types import SimpleNamespace

    from backend.app.generators import schedulers

    original = SimpleNamespace(config={})
    pipe = SimpleNamespace(scheduler=original)
    assert schedulers.apply(pipe, "default", model="Tongyi-MAI/Z-Image-Turbo") == "default"
    assert pipe.scheduler is original
