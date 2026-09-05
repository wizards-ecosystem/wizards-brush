"""Adapters that diffusers cannot take as-is, and what has to happen to them.

Two real files in this library drove these cases, and they fail in opposite ways:

  * kohya `.alpha` scalars — diffusers consumes the lora_A/lora_B pair, leaves
    the alpha behind, then refuses the whole load because the state dict
    "should be empty at this point". A loud failure, easy to notice.
  * the reference Z-Image fused `attention.qkv` — diffusers keeps to_q/to_k/to_v
    separate, so those tensors have nowhere to land. PEFT does NOT fail on this.
    It reports them as unexpected keys, applies everything else, and the adapter
    half-works. That is the dangerous one: the image just looks weakly styled and
    nothing says why.

These run without torch, so a stub stands in for a tensor. It carries only the
surface the converter touches — shape, row slicing, clone, scalar multiply —
which is also a useful check that the converter needs nothing more than that.
"""
from __future__ import annotations

from backend.app.generators.loraconv import _fold_alphas, _split_fused_qkv


class _Fake:
    """A tensor-shaped stand-in. `rows` are identifiable so a split is checkable."""

    def __init__(self, rows, cols: int = 4, scale: float = 1.0):
        self.rows = list(rows)
        self.cols = cols
        self.scale = scale

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.rows), self.cols)

    def clone(self) -> _Fake:
        return _Fake(self.rows, self.cols, self.scale)

    def reshape(self, *_):
        return [self.scale]

    def __mul__(self, k: float) -> _Fake:
        return _Fake(self.rows, self.cols, self.scale * k)

    def __getitem__(self, s) -> _Fake:
        return _Fake(self.rows[s], self.cols, self.scale)


def _lora(base: str, rank: int, out_rows: int, alpha: float | None = None) -> dict:
    sd = {
        f"{base}.lora_A.weight": _Fake(range(rank)),
        f"{base}.lora_B.weight": _Fake(range(out_rows)),
    }
    if alpha is not None:
        sd[f"{base}.alpha"] = _Fake([0], 1, scale=alpha)
    return sd


def test_alpha_equal_to_rank_drops_the_key_and_changes_nothing_else():
    """The common case in this library: alpha == rank, so the scale is 1.0 and
    only the redundant key is in the way."""
    sd = _lora("m", rank=64, out_rows=8, alpha=64.0)
    out, folded = _fold_alphas(sd)
    assert not [k for k in out if k.endswith(".alpha")]
    assert folded == 0, "a scale of 1.0 must not touch the weights"
    assert out["m.lora_B.weight"].scale == 1.0


def test_alpha_below_rank_rescales_the_adapter():
    """Dropping the alpha without folding would silently strengthen the adapter
    fourfold here — a wrong image rather than an error, which is why it folds."""
    sd = _lora("m", rank=64, out_rows=8, alpha=16.0)
    out, folded = _fold_alphas(sd)
    assert folded == 1
    assert out["m.lora_B.weight"].scale == 16.0 / 64.0


def test_an_alpha_with_no_matching_factors_is_simply_dropped():
    out, folded = _fold_alphas({"stray.alpha": _Fake([0], 1, scale=8.0)})
    assert out == {} and folded == 0


def test_fused_qkv_splits_into_three_with_a_shared_down_projection():
    """lora_A is one down projection feeding all three, and lora_B's rows ARE the
    fused output — so q, k and v are exact row-blocks of it, not an estimate."""
    sd = _lora("layers.0.attention.qkv", rank=8, out_rows=12)
    out, converted = _split_fused_qkv(sd)
    assert converted == 1
    assert not [k for k in out if "attention.qkv" in k], "the fused key must be gone"

    for part in ("to_q", "to_k", "to_v"):
        assert out[f"layers.0.attention.{part}.lora_A.weight"].rows == list(range(8))

    # Disjoint, contiguous, and covering every row of the fused projection.
    blocks = [out[f"layers.0.attention.{p}.lora_B.weight"].rows
              for p in ("to_q", "to_k", "to_v")]
    assert blocks == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]]


def test_the_output_projection_is_renamed_to_the_diffusers_name():
    sd = _lora("layers.0.attention.out", rank=4, out_rows=4)
    out, converted = _split_fused_qkv(sd)
    assert converted == 1
    assert "layers.0.attention.to_out.0.lora_A.weight" in out
    assert not [k for k in out if ".attention.out.lora_" in k]


def test_an_adapter_already_in_diffusers_layout_is_left_alone():
    """Two of the three Z-Image adapters here already use to_q/to_k/to_v. Touching
    them would be a way to break what currently works."""
    sd: dict = {}
    for part in ("to_q", "to_k", "to_v", "to_out.0"):
        sd |= _lora(f"layers.0.attention.{part}", rank=64, out_rows=64)
    out, converted = _split_fused_qkv(dict(sd))
    assert converted == 0
    assert set(out) == set(sd)


def test_a_fused_block_that_does_not_divide_by_three_is_left_intact():
    """Better to leave a tensor diffusers will complain about than to invent a
    split that silently mixes q, k and v."""
    sd = _lora("layers.0.attention.qkv", rank=8, out_rows=10)
    out, converted = _split_fused_qkv(sd)
    assert converted == 0
    assert "layers.0.attention.qkv.lora_A.weight" in out
    assert not [k for k in out if "to_q" in k]
