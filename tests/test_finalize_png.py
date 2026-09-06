"""Tests for scripts/finalize_png.py — synthetic frame-transfer smear + a bright target."""

import json
import math
import struct
from array import array

import finalize_png
import pytest

W = H = 128
SKY = 100.0
SMEAR_COLS = range(58, 70)      # a vertical band
SMEAR_DN = 900.0
PLANET_ROW, PLANET_COL, PLANET_R = 88, 64, 9
PLANET_DN = 60000.0


def _synthetic():
    data = array("f", [SKY] * (W * H))
    for ln in range(H):
        for s in range(W):
            i = ln * W + s
            if s in SMEAR_COLS:
                data[i] += SMEAR_DN
            if (ln - PLANET_ROW) ** 2 + (s - PLANET_COL) ** 2 <= PLANET_R ** 2:
                data[i] = PLANET_DN
    return data


def test_estimate_smear_picks_up_the_band_not_the_sky():
    smear = finalize_png.estimate_smear(_synthetic(), W, H, edge=32)
    assert smear[10] == pytest.approx(SKY, abs=1.0)          # clean column -> ~sky
    assert smear[64] == pytest.approx(SKY + SMEAR_DN, abs=1.0)  # smeared column


def test_finalize_removes_streak_and_keeps_target():
    px, stats = finalize_png.finalize(_synthetic(), W, H, edge=32)
    assert len(px) == W * H

    def at(ln, s):
        return px[ln * W + s]

    # Sky in a smeared column should end up as dark as sky in a clean column.
    assert abs(at(20, 64) - at(20, 10)) <= 4
    assert at(20, 64) < 40

    # The target is bright.
    assert at(PLANET_ROW, PLANET_COL) > 220

    assert stats["display_stretch"] == "asinh"
    assert stats["smear_max_dn"] > 500
    assert stats["null_pixels"] == 0


def test_null_and_saturation_sentinels():
    data = _synthetic()
    data[0] = -3.4e38          # ISIS NULL
    data[W * H - 1] = 3.4e38   # ISIS HRS
    px, stats = finalize_png.finalize(data, W, H, edge=32)
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
    # realistic DN values; wrong byte order would read as absurd magnitudes
    src = array("f", [float(v) for v in (120.5, 900.0, 4321.0, 55.0)])
    p = tmp_path / "img.raw"
    p.write_bytes(src.tobytes())
    assert list(finalize_png.read_raw_f32(p, 2, 2, endian="auto")) == list(src)

    swapped = array("f", src)
    swapped.byteswap()
    p.write_bytes(swapped.tobytes())
    got = finalize_png.read_raw_f32(p, 2, 2, endian="auto")
    assert list(got) == pytest.approx(list(src))


def test_read_raw_f32_rejects_short_file(tmp_path):
    p = tmp_path / "short.raw"
    p.write_bytes(b"\x00" * 8)
    with pytest.raises(ValueError):
        finalize_png.read_raw_f32(p, 10, 10, endian="little")


def test_main_writes_png_and_stats(tmp_path):
    src = _synthetic()
    raw = tmp_path / "in.raw"
    raw.write_bytes(src.tobytes())
    out = tmp_path / "sub" / "out.png"
    rc = finalize_png.main(
        [str(raw), str(out), "--samples", str(W), "--lines", str(H), "--edge", "32"]
    )
    assert rc == 0
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", out.read_bytes()[16:24])
    assert (w, h) == (W, H)
    stats = json.loads((tmp_path / "sub" / "out.png.stats.json").read_text())
    assert stats["samples"] == W and stats["lines"] == H


def test_finalize_raises_when_all_null():
    data = array("f", [math.nan] * (W * H))
    with pytest.raises(RuntimeError):
        finalize_png.finalize(data, W, H, edge=32)
