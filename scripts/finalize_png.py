#!/usr/bin/env python3
"""Turn a calibrated ISIS cube (dumped to raw float32 by ``isis2raw``) into a
display-ready PNG.

Pure Python standard library — no ISIS, no numpy. Runs on the CI runner right
after the ISIS calibration step, and is fully testable locally.

Pipeline:

1. Read a raw 32-bit float image (single band, band-sequential) as written by
   ``isis2raw ... bittype=REAL``.
2. **Readout-smear correction.** ONC-W2 is a shutterless frame-transfer CCD, so a
   bright source paints a vertical band down its column while the frame is
   clocked off the sensor. ISIS ``hyb2onccal`` does *not* remove this ("Smear
   correction is not currently provided, as we do not have the readout time").
   We estimate the per-column smear + sky pedestal from the top/bottom edge rows
   (always sky for the 2015 Earth-flyby set — Earth never reaches the frame
   edges) and subtract it column-by-column.
3. **asinh display stretch.** The nav exposures drive Earth to near-saturation;
   a linear stretch renders it as a white blob. asinh compresses the bright end
   so the disk and terminator are legible.
4. Write an 8-bit grayscale PNG, plus a ``<out>.stats.json`` sidecar for the
   manifest / debugging.
"""

from __future__ import annotations

import argparse
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
    raw = Path(path).read_bytes()
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


def estimate_smear(data: array, samples: int, lines: int, edge: int) -> list[float]:
    """Per-column (sample) smear + background pedestal, from the edge rows."""
    edge = max(1, min(edge, lines // 2))
    edge_lines = list(range(edge)) + list(range(lines - edge, lines))
    smear = [0.0] * samples
    for s in range(samples):
        col = []
        for ln in edge_lines:
            v = data[ln * samples + s]
            if math.isfinite(v) and SANE_LO < v < SANE_HI:
                col.append(v)
        smear[s] = _median(col) if col else 0.0
    return smear


def finalize(
    data: array,
    samples: int,
    lines: int,
    *,
    edge: int = 64,
    black_pct: float = 1.0,
    white_pct: float = 99.9,
    soft_frac: float = 0.02,
) -> tuple[bytearray, dict]:
    smear = estimate_smear(data, samples, lines, edge)

    corrected = array("f", bytes(4 * samples * lines))
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
                sat_count += 1
                continue
            cv = v - smear[s]
            corrected[row + s] = cv
            valid.append(cv)

    if not valid:
        raise RuntimeError("no valid pixels after smear correction")

    valid.sort()
    black = _percentile(valid, black_pct)
    white = _percentile(valid, white_pct)
    if white <= black:
        white = black + 1.0
    soft = max((white - black) * soft_frac, 1e-6)
    denom = math.asinh((white - black) / soft)

    px = bytearray(samples * lines)
    for i in range(samples * lines):
        cv = corrected[i]
        if cv != cv:  # NaN -> NULL
            px[i] = 0
        elif cv == math.inf:  # hard saturation
            px[i] = 255
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
        "smear_median_dn": round(_median(smear), 3),
        "smear_max_dn": round(max(smear), 3),
        "black_dn": round(black, 3),
        "white_dn": round(white, 3),
        "asinh_softening_dn": round(soft, 3),
        "null_pixels": null_count,
        "saturated_pixels": sat_count,
        "display_stretch": "asinh",
        "smear_correction": "per-column edge-row background subtraction",
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
    p.add_argument("--edge", type=int, default=64, help="edge rows used to estimate smear")
    p.add_argument("--black-percentile", type=float, default=1.0)
    p.add_argument("--white-percentile", type=float, default=99.9)
    p.add_argument("--soft-frac", type=float, default=0.02,
                   help="asinh softening as a fraction of (white-black)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data = read_raw_f32(args.infile, args.samples, args.lines, args.endian)
    px, stats = finalize(
        data, args.samples, args.lines,
        edge=args.edge,
        black_pct=args.black_percentile,
        white_pct=args.white_percentile,
        soft_frac=args.soft_frac,
    )
    args.outfile.parent.mkdir(parents=True, exist_ok=True)
    write_gray_png(args.outfile, args.samples, args.lines, px)
    Path(str(args.outfile) + ".stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    print(f"wrote {args.outfile}  {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
