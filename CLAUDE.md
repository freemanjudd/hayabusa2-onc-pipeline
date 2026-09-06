# CLAUDE.md — working notes for this repo

Read this first. It captures decisions that are **not** obvious from the code and
that future sessions should not have to rediscover.

## What this project is

An end-to-end electro-optical (EO) imagery pipeline for a fixed, small set of
**Hayabusa2 ONC-W2** wide-angle camera frames from the **3 December 2015 Earth
gravity-assist flyby**. It ingests raw FITS, radiometrically calibrates with
**USGS ISIS**, exports PNGs + a JSON manifest, and serves a static web viewer on
GitHub Pages. Reference doc: `HYB2-EO-PP-2026-001` (project plan PDF, not in repo).

Learning project. Cost target **$0**. Public repo (unlocks free unlimited Actions
minutes + free Pages on a personal account).

## The one hard rule: ISIS and Docker never touch the local machine

ISIS + its data area is ~40–50 GB. It is **never** installed or run locally.
**All ISIS work happens inside GitHub Actions**, in a container built from
`docker/Dockerfile` and cached in GHCR.

Consequence — every file falls on one side of this boundary:

| Side | Files | How it's tested |
|---|---|---|
| **Local / pure** (Python stdlib or plain JS, no ISIS/Docker) | `scripts/download_data.py`, `scripts/make_manifest.py`, `web/**` | `pytest`, `python -m http.server -d web`, run directly |
| **CI container only** | `scripts/isis_pipeline.sh`, `docker/Dockerfile` | `bash -n`, `shellcheck`, `--dry-run`; real run only in Actions |

When adding code, keep it on the correct side. Do **not** add `astropy`, ISIS
Python bindings, or anything that would pull a heavy scientific stack into the
local scripts. The local scripts import only the standard library.

## Two-workflow split (why there are two, not one)

1. **`.github/workflows/build-isis-image.yml`** — manual (`workflow_dispatch`).
   Builds `docker/Dockerfile` and pushes to `ghcr.io/<owner>/<repo>/isis`.
   **Slow** (ISIS install can take >1 h). **Re-run only when the Dockerfile
   changes.**
2. **`.github/workflows/process-images.yml`** — manual (`workflow_dispatch`),
   inputs `frame_count` (default `"1"`) and `commit_results` (default `false`).
   **Pulls** the prebuilt image (fast — no ISIS rebuild), downloads source data,
   runs the ISIS pipeline, builds the manifest, uploads a workflow artifact, and
   *only if `commit_results: true`* commits `web/data/**` back to the branch.

Splitting them means the slow step isn't repeated on every pipeline run.

3. **`.github/workflows/pages.yml`** — deploys `web/` to GitHub Pages on push to
   `main` touching `web/**`.
4. **`.github/workflows/ci.yml`** — lint + unit tests for the pure-Python side and
   `shellcheck` for the ISIS script, on every push/PR. No ISIS.

## Data

- Source: NASA PDS SBN, bundle `urn:jaxa:darts:hyb2_onc::1.0` (DOI
  `10.17597/isas.darts/hyb2-00200`), mirrored at JAXA DARTS.
- Frame list: **`config/frames.txt`** — the single source of truth. 20 ONC-W2
  full-frame images, `hyb2_onc_20151203_*_w2f_l2a`, 00:00:06–09:29:58 UTC.
- Archive path:
  `https://sbnarchive.psi.edu/pds4/hayabusa2/hyb2_onc/data_raw/earth_swing-by/20151203/`
- Each frame = `<id>.fit` (2 MB raw FITS) + `<id>.xml` (PDS4 label).

## Constraints (do not violate)

- **Never commit** `*.fit` / `*.fits` / `*.cub` / raw or intermediate products to
  git. Only `web/data/manifest.json` + `web/data/images/*.png` (small, final) are
  committed. `.gitignore` enforces this — do not loosen it.
- **ISISDATA downloads in CI are scoped to Hayabusa2 only.** The pipeline runs
  `downloadIsisData hayabusa2 $ISISDATA` and `downloadIsisData base $ISISDATA` —
  **never** `downloadIsisData all` (that pulls the full multi-mission archive).
- The ISIS step cannot be watched live. Keep `isis_pipeline.sh` verbose
  (`set -x`, version banners, ISISDATA inventory, disk usage) and make the
  process-images workflow upload logs with `if: always()`.
- `process-images.yml` must default `frame_count` to 1 so the first run is a
  cheap test, not an hour-long mystery.

## Layout

```
config/frames.txt          frame IDs (source of truth)
scripts/download_data.py    PURE PYTHON — fetch .fit + .xml from PDS  -> data/raw/
scripts/isis_pipeline.sh    CI ONLY — hyb2onc2isis -> spiceinit -> hyb2onccal -> isis2std
scripts/make_manifest.py    PURE PYTHON — PDS4 labels + PNGs -> web/data/manifest.json
docker/Dockerfile           ISIS environment
web/                        static viewer (index.html / style.css / app.js)
web/data/                   THE ONLY committed pipeline output (manifest.json + images/)
tests/                      pytest for the pure-Python scripts; real label fixtures
```

## Local dev quickstart

```bash
python -m pytest                              # unit tests, no network
python scripts/download_data.py --limit 2     # pull 2 real frames into data/ (gitignored)
python scripts/make_manifest.py --help        # manifest builder
python -m http.server -d web 8000             # view the site at localhost:8000
bash -n scripts/isis_pipeline.sh              # syntax-check the ISIS script (can't run it here)
bash scripts/isis_pipeline.sh --dry-run data/raw web/data/images
```

## Status / TODO

Repo: <https://github.com/freemanjudd/hayabusa2-onc-pipeline>
GHCR image: `ghcr.io/freemanjudd/hayabusa2-onc-pipeline/isis`

- [x] GitHub repo created + pushed (`freemanjudd/hayabusa2-onc-pipeline`, Public)
- [x] Repo Settings → Actions → Workflow permissions → Read and write
- [x] Pages → Source: GitHub Actions
- [ ] `build-isis-image.yml` run once → image in GHCR
- [ ] `process-images.yml` run with `frame_count=1`, then `20`, then `commit_results=true`
- [ ] Pages deploy verified → live URL recorded here and in README
- Live site: _not published yet_ (expected `https://freemanjudd.github.io/hayabusa2-onc-pipeline/`)
