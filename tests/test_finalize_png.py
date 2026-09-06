"""Tests for scripts/finalize_png.py — synthetic frame-transfer smear + a bright target."""

import json
import math
import struct
from array import array

import finalize_png
import pytest

W = H = 160
SKY = 100.0


def _noise(ln, s):
    # deterministic, ~+/-6 DN, so the sky has a real sigma
    return ((ln * 131 + s * 977) % 13) - 6


def _base(smear_cols=range(74, 86), smear_dn=900.0):
    data = array("f", [0.0] * (W * H))
    for ln in range(H):
        for s in range(W):
            v = SKY + _noise(ln, s)
            if s in smear_cols:
                v += smear_dn
            data[ln * W + s] = v
    return data


def _add_disk(data, row, col, r, value):
    for ln in range(H):
        for s in range(W):
            if (ln - row) ** 2 + (s - col) ** 2 <= r * r:
                data[ln * W + s] = value
    return data


# --- helpers --------------------------------------------------------------

def test_percentile_interpolates():
    vals = [0.0, 10.0, 20.0, 30.0, 40.0]
    assert finalize_png._percentile(vals, 0) == 0.0
    assert finalize_png._percentile(vals, 100) == 40.0
    assert finalize_png._percentile(vals, 50) == 20.0


def test_smooth_widens_a_spike():
    seq = [0.0] * 10
    seq[5] = 9.0
    out = finalize_png._smooth(seq, 3)
    assert out[5] == pytest.approx(3.0)
    assert out[4] == pytest.approx(3.0)
    assert out[0] == 0.0


def test_detect_ceiling_needs_a_pile_up():
    flat = array("f", [100.0] * 1000 + [500.0] * 60)   # 60 px at the top
    assert finalize_png._detect_ceiling(flat) == 500.0
    lone = array("f", [100.0] * 1000 + [500.0])        # a single bright pixel
    assert finalize_png._detect_ceiling(lone) is None


def test_estimate_sky_and_smear():
    sky, sigma, smear = finalize_png.estimate_sky_and_smear(_base(), W, H, edge=40, smooth=1)
    assert sky == pytest.approx(SKY, abs=3.0)
    assert sigma > 1.0
    assert smear[10] == pytest.approx(0.0, abs=3.0)      # clean column
    assert smear[80] == pytest.approx(900.0, abs=3.0)    # smeared column


def test_edge_zero_disables_smear_correction():
    sky, sigma, smear = finalize_png.estimate_sky_and_smear(_base(), W, H, edge=0, smooth=1)
    assert sky == 0.0 and sigma == 0.0
    assert set(smear) == {0.0}


# --- finalize ------------------------------------------------------------

def test_finalize_removes_streak_and_keeps_unsaturated_target():
    data = _add_disk(_base(smear_dn=900.0), row=110, col=80, r=10, value=8000.0)
    px, stats = finalize_png.finalize(data, W, H, edge=40)

    def at(ln, s):
        return px[ln * W + s]

    # sky in a smeared column ends up as dark as sky in a clean column
    assert abs(at(30, 80) - at(30, 12)) <= 4
    assert at(30, 80) < 25
    assert at(110, 80) > 210                     # target still bright
    assert stats["saturation_ceiling_dn"] == 8000.0
    assert stats["dead_columns"] == 0
    assert stats["null_pixels"] == 0


def test_finalize_forces_saturated_target_white():
    data = _add_disk(_base(smear_dn=200.0), row=80, col=80, r=9, value=6974.0)
    px, stats = finalize_png.finalize(data, W, H, edge=40)
    assert stats["saturation_ceiling_dn"] == pytest.approx(6974.0)
    assert stats["saturated_pixels"] >= 200
    assert px[80 * W + 80] == 255


def test_finalize_inpaints_smear_saturated_columns():
    # a smear band that reaches the ceiling in its core columns
    data = array("f", [0.0] * (W * H))
    for ln in range(H):
        for s in range(W):
            v = SKY + _noise(ln, s)
            if 76 <= s <= 84:
                v = 6974.0                       # smear saturates these columns
            elif 70 <= s <= 90:
                v += 1500.0
            data[ln * W + s] = v
    px, stats = finalize_png.finalize(data, W, H, edge=40)
    assert stats["dead_columns"] >= 3
    # inpainted sky columns should read dark, like their neighbours -- not a stripe
    for s in range(78, 83):
        assert abs(px[30 * W + s] - px[30 * W + 70]) <= 8


def test_finalize_wide_dead_band_across_saturated_target():
    """The near-approach case: a big saturated disk with smear saturating a wide
    central band. The band must fill white *through the disk* and black above it."""
    ceiling = SKY + 2600.0                                # matches the real data
    data = array("f", [0.0] * (W * H))
    cx, cy, r = 80, 95, 34
    for ln in range(H):
        for s in range(W):
            v = SKY + _noise(ln, s)
            if abs(s - cx) < 40:                          # smear, saturating near core
                v = min(ceiling, v + 6000 * (1 - abs(s - cx) / 40))
            if (ln - cy) ** 2 + (s - cx) ** 2 <= r * r:   # saturated disk
                v = ceiling
            data[ln * W + s] = v
    px, stats = finalize_png.finalize(data, W, H, edge=40)
    assert stats["dead_columns"] > 10
    assert px[cy * W + cx] == 255                          # disk centre: white
    assert px[10 * W + cx] < 30                            # sky above the disk: black


def test_null_and_hard_saturation_sentinels():
    data = _add_disk(_base(smear_dn=100.0), row=80, col=80, r=8, value=3000.0)
    data[0] = -3.4e38          # ISIS NULL
    data[W * H - 1] = 3.4e38   # ISIS HRS (> SAT_ABOVE)
    px, stats = finalize_png.finalize(data, W, H, edge=40)
    assert px[0] == 0
    assert px[-1] == 255
    assert stats["null_pixels"] == 1


def test_finalize_raises_when_all_null():
    data = array("f", [math.nan] * (W * H))
    with pytest.raises(RuntimeError):
        finalize_png.finalize(data, W, H, edge=40)


# --- raw IO ------------------------------------------------------------

def test_read_raw_f32_endianness(tmp_path):
    src = array("f", [1.0, 2.0, 3.0, 4.0])
    p = tmp_path / "img.raw"
    p.write_bytes(src.tobytes())
    assert list(finalize_png.read_raw_f32(p, 2, 2, endian="little")) == [1.0, 2.0, 3.0, 4.0]

    swapped = array("f", src)
    swapped.byteswap()
    p.write_bytes(swapped.tobytes())
    assert list(finalize_png.read_raw_f32(p, 2, 2, endian="big")) == [1.0, 2.0, 3.0, 4.0]


def test_read_raw_f32_auto_detects_byte_order(tmp_path):
    src = array("f", [120.5, 900.0, 4321.0, 55.0])
    p = tmp_path / "img.raw"
    p.write_bytes(src.tobytes())
    assert list(finalize_png.read_raw_f32(p, 2, 2, endian="auto")) == list(src)

    swapped = array("f", src)
    swapped.byteswap()
    p.write_bytes(swapped.tobytes())
    assert list(finalize_png.read_raw_f32(p, 2, 2, endian="auto")) == pytest.approx(list(src))


def test_read_raw_f32_accepts_gzip(tmp_path):
    import gzip as _gz
    src = array("f", [10.0, 20.0, 30.0, 40.0])
    p = tmp_path / "img.raw.gz"
    p.write_bytes(_gz.compress(src.tobytes()))
    assert list(finalize_png.read_raw_f32(p, 2, 2, endian="little")) == [10.0, 20.0, 30.0, 40.0]


def test_read_raw_f32_rejects_short_file(tmp_path):
    p = tmp_path / "short.raw"
    p.write_bytes(b"\x00" * 8)
    with pytest.raises(ValueError):
        finalize_png.read_raw_f32(p, 10, 10, endian="little")


def test_main_writes_png_and_stats(tmp_path):
    raw = tmp_path / "in.raw"
    raw.write_bytes(_add_disk(_base(), 110, 80, 10, 8000.0).tobytes())
    out = tmp_path / "sub" / "out.png"
    rc = finalize_png.main(
        [str(raw), str(out), "--samples", str(W), "--lines", str(H), "--edge", "40"]
    )
    assert rc == 0
    blob = out.read_bytes()
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", blob[16:24]) == (W, H)
    stats = json.loads((tmp_path / "sub" / "out.png.stats.json").read_text())
    assert stats["samples"] == W and stats["lines"] == H
