#!/usr/bin/env python3
"""Turn a calibrated ISIS cube (dumped to raw float32 by ``isis2raw``) into a
display-ready PNG.

Pure Python standard library — no ISIS, no numpy. Runs on the CI runner right
after the ISIS calibration step, and is fully testable locally.

Pipeline:

1. Read a raw 32-bit float image (single band, band-sequential) as written by
   ``isis2raw ... bittype=32BIT``. Also accepts a ``.gz`` of the same.
2. **Readout-smear correction.** ONC-W2 is a shutterless frame-transfer CCD, so a
   bright source paints a vertical band down its column while the frame is
   clocked off the sensor. ISIS ``hyb2onccal`` does *not* remove this ("Smear
   correction is not currently provided, as we do not have the readout time").
   We take the sky+bias pedestal as the global median of the top/bottom edge
   rows (always sky here — Earth never reaches the frame edges), take each
   column's *excess* over that as its smear, and subtract ``sky + smear[column]``
   from every pixel. Columns where the smear itself hits the saturation ceiling
   carry no recoverable signal and are inpainted from their neighbours.
3. **asinh display stretch.** The nav exposures drive Earth to near-saturation;
   a linear stretch renders it as a white blob. asinh compresses the bright end
   so the disk and terminator are legible. Genuinely saturated target pixels are
   forced white.
4. Write an 8-bit grayscale PNG, plus a ``<out>.stats.json`` sidecar for the
   manifest / debugging.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import struct
import sys
import zlib
from array import array
from pathlib import Path

# ISIS special-pixel sentinels in 32-bit float rasters are far outside any real
# DN range; treat anything past these fences as NULL / saturated.
NULL_BELOW = -1e30
SAT_ABOVE = 1e30
# Plausible post-calibration DN window used to reject garbage when estimating smear.
SANE_LO, SANE_HI = -5.0e4, 5.0e6


def _junk_fraction(data: array) -> float:
    """Fraction of pixels that look like a wrong-byte-order misread.

    Byte-swapped float32 typically decodes to denormal-tiny or wildly huge
    magnitudes rather than real DN values (which sit in the ~1..1e6 range) or
    the ISIS special-pixel sentinels.
    """
    n = len(data)
    step = max(1, n // 4096)
    junk = tot = 0
    for i in range(0, n, step):
        v = data[i]
        tot += 1
        if not math.isfinite(v):
            continue  # NaN/inf sentinels are fine
        a = abs(v)
        if a == 0.0 or a >= SAT_ABOVE:
            continue  # exact zero or ISIS HRS/LRS sentinel magnitude
        if a < 1e-3 or a > 1e7:
            junk += 1
    return junk / tot if tot else 1.0


def read_raw_f32(path: Path, samples: int, lines: int, endian: str) -> array:
    path = Path(path)
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    expected = samples * lines * 4
    if len(raw) < expected:
        raise ValueError(
            f"{path}: expected >= {expected} bytes for {samples}x{lines} float32, got {len(raw)}"
        )

    def decode(order: str) -> array:
        a = array("f")
        a.frombytes(raw[:expected])
        if order != sys.byteorder:
            a.byteswap()
        return a

    if endian == "auto":
        as_host = decode(sys.byteorder)
        if _junk_fraction(as_host) <= 0.05:
            return as_host
        other = "big" if sys.byteorder == "little" else "little"
        as_other = decode(other)
        if _junk_fraction(as_other) < _junk_fraction(as_host):
            sys.stderr.write(f"finalize_png: raw data looks {other}-endian; byte-swapped\n")
            return as_other
        return as_host

    return decode(endian)


def _median(values: list[float]) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    values = sorted(values)
    mid = n // 2
    return values[mid] if n % 2 else 0.5 * (values[mid - 1] + values[mid])


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def _mad_sigma(values: list[float], center: float) -> float:
    if not values:
        return 0.0
    dev = sorted(abs(v - center) for v in values)
    return 1.4826 * dev[len(dev) // 2]


def _smooth(seq: list[float], window: int) -> list[float]:
    """Simple centred moving average (odd window)."""
    if window <= 1:
        return list(seq)
    half = window // 2
    n = len(seq)
    out = [0.0] * n
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out[i] = sum(seq[lo:hi]) / (hi - lo)
    return out


def estimate_sky_and_smear(
    data: array, samples: int, lines: int, edge: int, smooth: int
) -> tuple[float, float, list[float]]:
    """Global sky level + noise, and the per-column *excess* smear over sky.

    The edge rows (first/last ``edge`` lines) are always sky for this data set, so
    their global median is the sky+bias pedestal and each column's median minus
    that is the additive readout-smear contribution for that column.
    """
    edge = max(1, min(edge, lines // 2)) if edge > 0 else 0
    if edge == 0:
        return 0.0, 0.0, [0.0] * samples

    edge_lines = list(range(edge)) + list(range(lines - edge, lines))
    all_edge: list[float] = []
    cols: list[list[float]] = [[] for _ in range(samples)]
    for ln in edge_lines:
        row = ln * samples
        for s in range(samples):
            v = data[row + s]
            if math.isfinite(v) and SANE_LO < v < SANE_HI:
                all_edge.append(v)
                cols[s].append(v)

    sky = _median(all_edge)
    sigma = _mad_sigma(all_edge, sky)
    excess = [max(0.0, (_median(c) - sky) if c else 0.0) for c in cols]
    return sky, sigma, _smooth(excess, smooth)


def _detect_ceiling(data: array) -> float | None:
    """The DN value bright pixels pile up at, if the frame has a saturation clip."""
    hi = NULL_BELOW
    for v in data:
        if math.isfinite(v) and NULL_BELOW < v < SAT_ABOVE and v > hi:
            hi = v
    if hi <= NULL_BELOW:
        return None
    eps = max(1.0, abs(hi) * 1e-4)
    hits = sum(1 for v in data if math.isfinite(v) and v >= hi - eps)
    return hi if hits >= 50 else None


def _inpaint_columns(corrected: array, samples: int, lines: int, dead: list[int]) -> None:
    """Linearly interpolate whole dead columns from their nearest live neighbours."""
    if not dead:
        return
    deadset = set(dead)
    live = [s for s in range(samples) if s not in deadset]
    if not live:
        return
    for s in dead:
        left = max((x for x in live if x < s), default=None)
        right = min((x for x in live if x > s), default=None)
        for ln in range(lines):
            row = ln * samples
            if left is None:
                corrected[row + s] = corrected[row + right]
            elif right is None:
                corrected[row + s] = corrected[row + left]
            else:
                f = (s - left) / (right - left)
                corrected[row + s] = corrected[row + left] * (1 - f) + corrected[row + right] * f


def finalize(
    data: array,
    samples: int,
    lines: int,
    *,
    edge: int = 64,
    black_sigma: float = 5.0,
    white_pct: float = 99.9,
    soft_frac: float = 0.055,
    smear_smooth: int = 1,
) -> tuple[bytearray, dict]:
    sky, sigma, smear = estimate_sky_and_smear(data, samples, lines, edge, smear_smooth)
    ceiling = _detect_ceiling(data)
    ceil_eps = max(1.0, abs(ceiling) * 1e-4) if ceiling is not None else 0.0

    # Columns where the smear itself reaches saturation carry no recoverable
    # signal (sky and target alike are clipped) -> inpaint them for display.
    dead_cols: list[int] = []
    if ceiling is not None:
        limit = (ceiling - sky) - max(4.0 * sigma, 20.0)
        dead_cols = [s for s in range(samples) if smear[s] >= limit]
    deadset = set(dead_cols)

    corrected = array("f", bytes(4 * samples * lines))
    bright = bytearray(samples * lines)  # genuine saturated target pixels -> white
    valid: list[float] = []
    null_count = sat_count = 0
    for ln in range(lines):
        row = ln * samples
        for s in range(samples):
            v = data[row + s]
            if not math.isfinite(v) or v <= NULL_BELOW:
                corrected[row + s] = math.nan
                null_count += 1
                continue
            if v >= SAT_ABOVE:
                corrected[row + s] = math.inf
                bright[row + s] = 1
                sat_count += 1
                continue
            cv = v - sky - smear[s]
            corrected[row + s] = cv
            if s not in deadset:
                valid.append(cv)
            if ceiling is not None and v >= ceiling - ceil_eps and s not in deadset:
                bright[row + s] = 1
                sat_count += 1

    _inpaint_columns(corrected, samples, lines, dead_cols)

    if not valid:
        raise RuntimeError("no valid pixels after smear correction")

    valid.sort()
    black = black_sigma * sigma if sigma > 0 else _percentile(valid, 50.0)
    white = _percentile(valid, white_pct)
    if white <= black:
        white = black + 1.0
    soft = max((white - black) * soft_frac, 1e-6)
    denom = math.asinh((white - black) / soft)

    px = bytearray(samples * lines)
    for i in range(samples * lines):
        cv = corrected[i]
        if bright[i] or cv == math.inf:
            px[i] = 255
        elif cv != cv:  # NaN -> NULL
            px[i] = 0
        else:
            t = math.asinh((cv - black) / soft) / denom
            if t <= 0.0:
                px[i] = 0
            elif t >= 1.0:
                px[i] = 255
            else:
                px[i] = int(t * 255 + 0.5)

    stats = {
        "samples": samples,
        "lines": lines,
        "sky_dn": round(sky, 3),
        "sky_sigma_dn": round(sigma, 3),
        "smear_max_dn": round(max(smear), 3),
        "saturation_ceiling_dn": round(ceiling, 3) if ceiling is not None else None,
        "dead_columns": len(dead_cols),
        "black_dn": round(black, 3),
        "white_dn": round(white, 3),
        "asinh_softening_dn": round(soft, 3),
        "null_pixels": null_count,
        "saturated_pixels": sat_count,
        "display_stretch": "asinh",
        "smear_correction": "per-column excess over sky (edge rows); saturated columns inpainted",
    }
    return px, stats


def write_gray_png(path: Path, width: int, height: int, px: bytearray) -> None:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + tag
            + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        raw.extend(px[y * width : (y + 1) * width])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit grayscale
    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("infile", type=Path, help="raw float32 image from isis2raw")
    p.add_argument("outfile", type=Path, help="PNG to write")
    p.add_argument("--samples", type=int, required=True, help="image width (ISIS Samples)")
    p.add_argument("--lines", type=int, required=True, help="image height (ISIS Lines)")
    p.add_argument("--endian", choices=["auto", "little", "big"], default="auto",
                   help="byte order of infile (isis2raw writes host order; 'auto' detects)")
    p.add_argument("--edge", type=int, default=64,
                   help="edge rows used to estimate sky + smear (0 disables smear correction)")
    p.add_argument("--black-sigma", type=float, default=5.0,
                   help="black point, in sky-noise sigmas above the (corrected) sky")
    p.add_argument("--white-percentile", type=float, default=99.9)
    p.add_argument("--soft-frac", type=float, default=0.055,
                   help="asinh softening as a fraction of (white-black)")
    p.add_argument("--smear-smooth", type=int, default=1,
                   help="moving-average window (columns) applied to the smear estimate")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data = read_raw_f32(args.infile, args.samples, args.lines, args.endian)
    px, stats = finalize(
        data, args.samples, args.lines,
        edge=args.edge,
        black_sigma=args.black_sigma,
        white_pct=args.white_percentile,
        soft_frac=args.soft_frac,
        smear_smooth=args.smear_smooth,
    )
    args.outfile.parent.mkdir(parents=True, exist_ok=True)
    write_gray_png(args.outfile, args.samples, args.lines, px)
    Path(str(args.outfile) + ".stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    print(f"wrote {args.outfile}  {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
