"""Memory and disk bounds for derived video processing."""
from __future__ import annotations

import sys
from types import SimpleNamespace


def test_rife_interpolation_streams_frames_instead_of_retaining_the_clip(monkeypatch, tmp_path):
    from backend.app.generators import postprocess as pp

    seen = {"alive": 0, "peak": 0, "written": 0}

    class Frame:
        def __init__(self):
            seen["alive"] += 1
            seen["peak"] = max(seen["peak"], seen["alive"])

        def __del__(self):
            seen["alive"] -= 1

        def __getitem__(self, _key):
            return self

    class Reader:
        def get_meta_data(self):
            return {"fps": 20.0}

        def iter_data(self):
            for _ in range(20):
                if seen["alive"] > 5:
                    raise AssertionError("input frames accumulated in memory")
                yield Frame()

        def close(self):
            return None

    class Writer:
        def append_data(self, _frame):
            seen["written"] += 1

        def close(self):
            return None

    fake_imageio = SimpleNamespace(
        get_reader=lambda *_args, **_kwargs: Reader(),
        get_writer=lambda *_args, **_kwargs: Writer(),
    )
    monkeypatch.setitem(sys.modules, "imageio", fake_imageio)
    monkeypatch.setattr(
        pp, "_load_rife",
        lambda: SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"]),
    )
    monkeypatch.setattr(pp, "_rife_middle", lambda _sess, _left, _right: Frame())

    pp.interpolate_video_rife(tmp_path / "in.mp4", tmp_path / "out.mp4", factor=2)

    assert seen["written"] == 39
    assert seen["peak"] <= 5
