"""Tests for scripts/finalize_png.py — synthetic frame-transfer smear + a bright target."""

import json
import math
import struct
from array import array

import finalize_png
import pytest

W = H = 160
SKY = 100.0
SMEAR_COLS = range(74, 86)          # a vertical band
SMEAR_DN = 900.0
PLANET_ROW, PLANET_COL, PLANET_R = 110, 80, 11
PLANET_DN = 60000.0


def _noise(ln, s):
    # deterministic, ~+/-6 DN, so the sky has a real sigma
    return ((ln * 131 + s * 977) % 13) - 6


def _synthetic():
    data = array("f", [0.0] * (W * H))
    for ln in range(H):
        for s in range(W):
            i = ln * W + s
            v = SKY + _noise(ln, s)
            if s in SMEAR_COLS:
                v += SMEAR_DN
            if (ln - PLANET_ROW) ** 2 + (s - PLANET_COL) ** 2 <= PLANET_R ** 2:
                v = PLANET_DN
            data[i] = v
    return data


def test_estimate_sky_and_smear():
    sky, sigma, smear = finalize_png.estimate_sky_and_smear(_synthetic(), W, H, edge=40, smooth=1)
    assert sky == pytest.approx(SKY, abs=3.0)
    assert sigma > 1.0
    assert smear[10] == pytest.approx(0.0, abs=3.0)          # clean column
    assert smear[80] == pytest.approx(SMEAR_DN, abs=3.0)     # smeared column


def test_finalize_removes_streak_and_keeps_target():
    px, stats = finalize_png.finalize(_synthetic(), W, H, edge=40, smear_smooth=1)
    assert len(px) == W * H

    def at(ln, s):
        return px[ln * W + s]

    # Sky in a smeared column ends up as dark as sky in a clean column.
    assert abs(at(30, 80) - at(30, 12)) <= 4
    assert at(30, 80) < 30

    assert at(PLANET_ROW, PLANET_COL) > 220        # target is bright
    assert stats["display_stretch"] == "asinh"
    assert stats["smear_max_dn"] > 500
    assert stats["sky_dn"] == pytest.approx(SKY, abs=3.0)
    assert stats["null_pixels"] == 0


def test_edge_zero_disables_smear_correction():
    sky, sigma, smear = finalize_png.estimate_sky_and_smear(_synthetic(), W, H, edge=0, smooth=1)
    assert sky == 0.0 and sigma == 0.0
    assert set(smear) == {0.0}


def test_smooth_widens_a_spike():
    seq = [0.0] * 10
    seq[5] = 9.0
    out = finalize_png._smooth(seq, 3)
    assert out[5] == pytest.approx(3.0)
    assert out[4] == pytest.approx(3.0)
    assert out[0] == 0.0


def test_null_and_saturation_sentinels():
    data = _synthetic()
    data[0] = -3.4e38          # ISIS NULL
    data[W * H - 1] = 3.4e38   # ISIS HRS
    px, stats = finalize_png.finalize(data, W, H, edge=40)
    assert px[0] == 0
    assert px[-1] == 255
    assert stats["null_pixels"] == 1
    assert stats["saturated_pixels"] == 1


def test_percentile_interpolates():
    vals = [0.0, 10.0, 20.0, 30.0, 40.0]
    assert finalize_png._percentile(vals, 0) == 0.0
    assert finalize_png._percentile(vals, 100) == 40.0
    assert finalize_png._percentile(vals, 50) == 20.0


def test_read_raw_f32_endianness(tmp_path):
    src = array("f", [1.0, 2.0, 3.0, 4.0])
    p = tmp_path / "img.raw"
    p.write_bytes(src.tobytes())
    same = finalize_png.read_raw_f32(p, 2, 2, endian="little")
    assert list(same) == [1.0, 2.0, 3.0, 4.0]

    swapped = array("f", src)
    swapped.byteswap()
    p.write_bytes(swapped.tobytes())
    fixed = finalize_png.read_raw_f32(p, 2, 2, endian="big")
    assert list(fixed) == [1.0, 2.0, 3.0, 4.0]


def test_read_raw_f32_auto_detects_byte_order(tmp_path):
    src = array("f", [120.5, 900.0, 4321.0, 55.0])
    p = tmp_path / "img.raw"
    p.write_bytes(src.tobytes())
    assert list(finalize_png.read_raw_f32(p, 2, 2, endian="auto")) == list(src)

    swapped = array("f", src)
    swapped.byteswap()
    p.write_bytes(swapped.tobytes())
    assert list(finalize_png.read_raw_f32(p, 2, 2, endian="auto")) == pytest.approx(list(src))


def test_read_raw_f32_rejects_short_file(tmp_path):
    p = tmp_path / "short.raw"
    p.write_bytes(b"\x00" * 8)
    with pytest.raises(ValueError):
        finalize_png.read_raw_f32(p, 10, 10, endian="little")


def test_main_writes_png_and_stats(tmp_path):
    raw = tmp_path / "in.raw"
    raw.write_bytes(_synthetic().tobytes())
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


def test_finalize_raises_when_all_null():
    data = array("f", [math.nan] * (W * H))
    with pytest.raises(RuntimeError):
        finalize_png.finalize(data, W, H, edge=40)
