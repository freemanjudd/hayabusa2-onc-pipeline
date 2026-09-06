"""Tests for scripts/make_manifest.py — runs against the checked-in real labels."""

import json
from pathlib import Path

import make_manifest
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
FIRST = "hyb2_onc_20151203_000006_w2f_l2a"
LAST = "hyb2_onc_20151203_092958_w2f_l2a"


def test_parse_label_extracts_expected_fields():
    rec = make_manifest.parse_label(FIXTURES / f"{FIRST}.xml")
    assert rec["id"] == FIRST
    assert rec["start_utc"] == "2015-12-03T00:00:06.637Z"
    assert rec["target"] == "Earth"
    assert rec["instrument"] == "HAYABUSA2_ONC-W2"
    assert rec["mission_phase"] == "Earth Swing-by Phase"
    assert rec["exposure_s"] == pytest.approx(0.0041, rel=1e-6)
    assert rec["lines"] == 1024
    assert rec["samples"] == 1024
    assert rec["earth_distance_km"] == pytest.approx(202859.0)
    assert rec["source_fit"] == f"{FIRST}.fit"
    assert rec["source_label_url"].endswith(f"{FIRST}.xml")


def test_build_manifest_orders_by_time_and_is_null_safe(tmp_path):
    manifest = make_manifest.build_manifest(
        FIXTURES, tmp_path / "no-images", allow_missing_png=True
    )
    ids = [img["id"] for img in manifest["images"]]
    assert ids == [FIRST, LAST], "images must be sorted by acquisition time"

    assert manifest["dataset"]["frame_count"] == 2
    assert manifest["dataset"]["mission"] == "Hayabusa2"
    assert manifest["dataset"]["generated_utc"].endswith("Z")

    for img in manifest["images"]:
        assert img["png"] is None  # allow_missing_png path
        # Earth got closer through the approach.
    assert manifest["images"][0]["earth_distance_km"] > manifest["images"][1]["earth_distance_km"]

    # Whole thing must be JSON-serialisable.
    json.dumps(manifest)


def test_build_manifest_picks_up_png_and_size(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / f"{FIRST}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)

    manifest = make_manifest.build_manifest(FIXTURES, images, allow_missing_png=True)
    by_id = {img["id"]: img for img in manifest["images"]}
    assert by_id[FIRST]["png"] == f"images/{FIRST}.png"
    assert by_id[FIRST]["png_bytes"] == 48
    assert by_id[LAST]["png"] is None


def test_build_manifest_excludes_frames_without_png_by_default(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / f"{LAST}.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    manifest = make_manifest.build_manifest(FIXTURES, images, allow_missing_png=False)
    assert [img["id"] for img in manifest["images"]] == [LAST]


def test_build_manifest_raises_when_no_labels(tmp_path):
    with pytest.raises(FileNotFoundError):
        make_manifest.build_manifest(tmp_path, tmp_path, allow_missing_png=True)


def test_main_writes_file(tmp_path):
    out = tmp_path / "manifest.json"
    rc = make_manifest.main(
        ["--labels", str(FIXTURES), "--images", str(tmp_path), "--out", str(out),
         "--allow-missing-png"]
    )
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["dataset"]["frame_count"] == 2
