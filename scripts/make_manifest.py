#!/usr/bin/env python3
"""Build ``web/data/manifest.json`` from PDS4 labels + exported PNGs.

Pure Python standard library only. Reads the ``<id>.xml`` PDS4 labels downloaded
by ``download_data.py`` and the ``<id>.png`` files produced by the ISIS pipeline,
and writes a single JSON file that the static web viewer consumes.

Every useful field the viewer shows comes straight from the label — including the
per-frame ``earth_distance_from_spacecraft`` (km), so no SPICE computation is
needed.

Examples
--------
    python scripts/make_manifest.py \
        --labels data/raw --images web/data/images --out web/data/manifest.json

    # local check against the checked-in fixtures (no PNGs required):
    python scripts/make_manifest.py \
        --labels tests/fixtures --images /tmp/none --out /tmp/manifest.json \
        --allow-missing-png
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LABELS = REPO_ROOT / "data" / "raw"
DEFAULT_IMAGES = REPO_ROOT / "web" / "data" / "images"
DEFAULT_OUT = REPO_ROOT / "web" / "data" / "manifest.json"

ARCHIVE_LABEL_BASE = (
    "https://sbnarchive.psi.edu/pds4/hayabusa2/hyb2_onc/"
    "data_raw/earth_swing-by/20151203/"
)

DATASET = {
    "mission": "Hayabusa2",
    "instrument": "ONC-W2",
    "event": "Earth gravity-assist flyby",
    "date": "2015-12-03",
    "source_bundle": "urn:jaxa:darts:hyb2_onc::1.0",
    "source_archive": "NASA PDS Small Bodies Node",
    "citation": (
        "Sugita, S. et al. (2022). Hayabusa2 ONC Bundle V1.0, "
        "urn:jaxa:darts:hyb2_onc::1.0, NASA Planetary Data System. "
        "https://doi.org/10.17597/isas.darts/hyb2-00200"
    ),
    "processing": "USGS ISIS: hyb2onc2isis -> spiceinit -> hyb2onccal -> isis2std",
}

log = logging.getLogger("make_manifest")


def _local(tag: str) -> str:
    """Strip the XML namespace from a tag."""
    return tag.split("}")[-1]


def _first_text(root: ET.Element, name: str) -> str | None:
    """Return the text of the first element whose local name is ``name``."""
    for el in root.iter():
        if _local(el.tag) == name:
            txt = (el.text or "").strip()
            if txt:
                return txt
    return None


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _axis_elements(root: ET.Element, axis_name: str) -> int | None:
    """Pull Line/Sample size out of the Array_2D_Image Axis_Array blocks."""
    for arr in root.iter():
        if _local(arr.tag) != "Axis_Array":
            continue
        name = None
        elements = None
        for child in arr:
            lname = _local(child.tag)
            if lname == "axis_name":
                name = (child.text or "").strip()
            elif lname == "elements":
                elements = (child.text or "").strip()
        if name == axis_name and elements:
            try:
                return int(elements)
            except ValueError:
                return None
    return None


def parse_label(path: Path) -> dict:
    """Extract the viewer-relevant fields from one PDS4 label."""
    root = ET.parse(path).getroot()
    frame_id = path.stem

    start = _first_text(root, "start_date_time")
    rec = {
        "id": frame_id,
        "start_utc": start,
        "stop_utc": _first_text(root, "stop_date_time"),
        "observation_utc": _first_text(root, "observation_date_time"),
        "target": None,  # filled from Target_Identification/name below
        "mission_phase": _first_text(root, "mission_phase_name"),
        "instrument": _first_text(root, "naif_instrument_name") or "HAYABUSA2_ONC-W2",
        "exposure_s": _float(_first_text(root, "exposure_duration")),
        "band_center_nm": _float(_first_text(root, "band_center")),
        "bandwidth_nm": _float(_first_text(root, "bandwidth")),
        "lines": _axis_elements(root, "Line"),
        "samples": _axis_elements(root, "Sample"),
        "earth_distance_km": _float(_first_text(root, "earth_distance_from_spacecraft")),
        "sun_distance_km": _float(_first_text(root, "sun_distance_from_spacecraft")),
        "processing_level": _first_text(root, "processing_level"),
        "source_fit": f"{frame_id}.fit",
        "source_label_url": f"{ARCHIVE_LABEL_BASE}{frame_id}.xml",
    }

    # Target name lives in Target_Identification/name specifically.
    for ti in root.iter():
        if _local(ti.tag) == "Target_Identification":
            rec["target"] = _first_text(ti, "name")
            break

    return rec


def build_manifest(
    labels_dir: Path,
    images_dir: Path,
    *,
    allow_missing_png: bool = False,
) -> dict:
    labels_dir = Path(labels_dir)
    images_dir = Path(images_dir)

    label_paths = sorted(labels_dir.glob("*_w2f_l2a.xml")) or sorted(labels_dir.glob("*.xml"))
    if not label_paths:
        raise FileNotFoundError(f"no PDS4 labels (*.xml) found in {labels_dir}")

    images: list[dict] = []
    skipped: list[str] = []
    for lp in label_paths:
        rec = parse_label(lp)
        png_path = images_dir / f"{rec['id']}.png"
        if png_path.is_file():
            rec["png"] = f"images/{rec['id']}.png"
            rec["png_bytes"] = png_path.stat().st_size
        elif allow_missing_png:
            rec["png"] = None
            rec["png_bytes"] = None
        else:
            skipped.append(rec["id"])
            log.warning("no PNG for %s (%s) — excluded", rec["id"], png_path)
            continue
        images.append(rec)

    # Order by acquisition time so the viewer shows Earth growing through approach.
    images.sort(key=lambda r: r["start_utc"] or r["id"])

    if not images:
        raise RuntimeError(
            "manifest would be empty: no labels had a matching PNG "
            "(pass --allow-missing-png for a labels-only dry run)"
        )
    if skipped:
        log.warning("%d frame(s) excluded for missing PNGs: %s", len(skipped), skipped)

    return {
        "dataset": {
            **DATASET,
            "frame_count": len(images),
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "images": images,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--labels", type=Path, default=DEFAULT_LABELS,
                   help=f"directory of <id>.xml PDS4 labels (default: {DEFAULT_LABELS})")
    p.add_argument("--images", type=Path, default=DEFAULT_IMAGES,
                   help=f"directory of <id>.png exports (default: {DEFAULT_IMAGES})")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help=f"output manifest path (default: {DEFAULT_OUT})")
    p.add_argument("--allow-missing-png", action="store_true",
                   help="include frames whose PNG is absent (png: null) — for local dry runs")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    manifest = build_manifest(
        args.labels, args.images, allow_missing_png=args.allow_missing_png
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2) + "\n")
    log.info("wrote %s (%d images)", args.out, manifest["dataset"]["frame_count"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
