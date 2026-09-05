"""Failure classification: OOM detection without torch, the invert-the-catch
idiom, actionable tips, and GPU recovery decisions."""
from __future__ import annotations

import pytest

from backend.app.errors import clears_gpu, is_oom, raise_non_oom, tip_for


class FakeCudaOOM(Exception):
    """Stands in for torch.cuda.OutOfMemoryError, which we cannot import here —
    the test suite runs without torch, which is the whole point of matching on
    the class name rather than the class object."""


FakeCudaOOM.__name__ = "OutOfMemoryError"


def test_detects_oom_by_class_name_without_importing_torch():
    import sys

    assert is_oom(FakeCudaOOM("CUDA out of memory"))
    assert "torch" not in sys.modules, "classification must not drag in the heavy stack"


def test_detects_oom_by_message():
    assert is_oom(RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB"))
    assert is_oom(RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"))
    assert is_oom(MemoryError("not enough memory"))


def test_ordinary_errors_are_not_oom():
    assert not is_oom(ValueError("bad model id"))
    assert not is_oom(RuntimeError("mat1 and mat2 shapes cannot be multiplied"))
    assert not is_oom(FileNotFoundError("model_index.json"))


def test_raise_non_oom_lets_memory_failures_through():
    raise_non_oom(RuntimeError("CUDA out of memory"))  # returns, does not raise


def test_raise_non_oom_reraises_everything_else():
    """The bug this exists to stop: a typo in a model id silently degrading to a
    slower path, with the real error never surfacing."""
    original = ValueError("Tongyi-MAI/Z-Image-Trubo does not exist")
    with pytest.raises(ValueError, match="Trubo"):
        raise_non_oom(original)


def test_raise_non_oom_preserves_the_original_exception():
    original = KeyError("transformer")
    try:
        raise_non_oom(original)
    except KeyError as caught:
        assert caught is original, "the original traceback must survive"
    else:
        pytest.fail("should have re-raised")


@pytest.mark.parametrize(("exc", "expect"), [
    (RuntimeError("CUDA out of memory"), "lower quality tier"),
    (RuntimeError("mat1 and mat2 shapes cannot be multiplied"), "different model"),
    (RuntimeError("ControlNet needs LOCAL_OFFLOAD=false"), "CPU offload"),
    (RuntimeError("Remote GPU is not connected — paste the URL"), "HTTPS URL"),
    (RuntimeError("Remote GPU lost this job — the worker was restarted"), "queue the job again"),
    (RuntimeError("401 Client Error: Unauthorized"), "Hugging Face token"),
    (RuntimeError("No space left on device"), "disk is full"),
])
def test_known_failures_get_actionable_guidance(exc, expect):
    assert expect.lower() in tip_for(exc).lower()


def test_unknown_failures_get_no_invented_guidance():
    """Empty is a valid answer. Guessing sends people chasing the wrong thing."""
    assert tip_for(RuntimeError("something entirely novel happened")) == ""


def test_remote_gpu_403_does_not_misdirect_to_hugging_face():
    tip = tip_for(RuntimeError("Remote GPU error 403: forbidden"), "image_colab")
    assert "remote gpu route" in tip.lower()
    assert "hugging face" not in tip.lower()


def test_hugging_face_auth_tip_points_to_the_real_configuration_surface():
    tip = tip_for(RuntimeError("403 GatedRepoError"), "image_local")
    assert "hf_token" in tip.lower()
    assert ".env" in tip.lower()
    assert "settings" not in tip.lower()


def test_unscoped_remote_403_gets_no_invented_model_access_advice():
    assert tip_for(RuntimeError("upstream returned 403"), "t2v") == ""


def test_oom_clears_the_gpu_but_a_config_error_does_not():
    """An OOM leaves the allocator fragmented, so the next job needs a clean slate.
    A bad LoRA does not, and freeing the model would cost a needless reload."""
    assert clears_gpu(RuntimeError("CUDA out of memory")) is True
    assert clears_gpu(RuntimeError("mat1 and mat2 shapes")) is False
    assert clears_gpu(RuntimeError("nothing recognisable")) is False


def test_remote_oom_never_evicts_the_local_resident_model():
    error = RuntimeError("CUDA out of memory")
    assert clears_gpu(error, "t2v") is False
    assert "remote accelerator" in tip_for(error, "t2v").lower()


def test_first_matching_rule_wins():
    """An OOM that also mentions CUDA must get the memory tip, not the driver one."""
    tip = tip_for(RuntimeError("CUDA error: CUDA out of memory"))
    assert "lower quality tier" in tip


# ---- severity bands -------------------------------------------------------
def test_bands_make_is_warning_a_threshold_not_a_list():
    """Adding a new warning kind must not require touching display code."""
    from backend.app.errors import Severity

    assert Severity.warning.is_warning
    assert Severity.notice.is_warning
    assert not Severity.fatal.is_warning
    assert not Severity.transient.is_warning


def test_only_transient_failures_are_retryable():
    from backend.app.errors import Severity

    assert Severity.transient.is_retryable
    assert not Severity.fatal.is_retryable
    assert not Severity.warning.is_retryable


def test_a_dropped_tunnel_is_transient_not_fatal():
    """The distinction that decides whether retrying is worth offering."""
    from backend.app.errors import Severity, classify

    got = classify(RuntimeError("Remote GPU connection lost: read timeout"))
    assert got.severity is Severity.transient
    assert got.is_retryable


def test_an_out_of_memory_failure_is_fatal_and_clears_the_gpu():
    from backend.app.errors import Severity, classify

    got = classify(RuntimeError("CUDA out of memory"))
    assert got.severity is Severity.fatal
    assert got.clears_gpu
    assert not got.is_retryable


def test_a_lora_that_did_not_apply_is_a_warning_the_job_still_succeeded():
    from backend.app.errors import Severity, classify

    got = classify(RuntimeError("could not activate LoRAs ['x']"))
    assert got.severity is Severity.warning
    assert got.is_warning, "the images exist; this is not a failure"


def test_an_unrecognised_failure_defaults_to_fatal_with_no_invented_tip():
    from backend.app.errors import Severity, classify

    got = classify(RuntimeError("something entirely novel"))
    assert got.severity is Severity.fatal
    assert got.tip == ""


def test_the_helpers_delegate_to_classify_rather_than_rewalking_the_rules():
    """tip_for and clears_gpu are now thin wrappers; they must not drift."""
    from backend.app.errors import classify, clears_gpu, tip_for

    for exc in (RuntimeError("CUDA out of memory"),
                RuntimeError("401 Client Error"),
                RuntimeError("nothing recognisable")):
        got = classify(exc)
        assert got.tip == tip_for(exc)
        assert got.clears_gpu == clears_gpu(exc)
