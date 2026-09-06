# Hayabusa2 ONC-W2 Earth-Flyby Imagery Pipeline

An end-to-end electro-optical imagery pipeline that takes **real Hayabusa2
ONC-W2** wide-angle camera frames from the **3 December 2015 Earth
gravity-assist flyby**, radiometrically calibrates them with
[USGS ISIS](https://github.com/DOI-USGS/ISIS3), and displays the results in a
small static web viewer.

The scientific processing software (ISIS, ~40 GB with its data area) is **never
installed locally** — it runs only inside GitHub Actions, packaged in a Docker
image. Everything you run on your own machine (data download, manifest
generation, the web viewer) is pure Python / JavaScript with no dependencies.

**Live viewer:** _not published yet_ &nbsp;·&nbsp; cost to run: **$0**

## How it works

```
config/frames.txt ──► download_data.py ──► data/raw/*.fit + *.xml   (local, gitignored)
                                             │
                          ┌──────────────────┘  (inside CI container only)
                          ▼
   hyb2onc2isis ──► spiceinit ──► hyb2onccal ──► isis2std ──► web/data/images/*.png
                                             │
                                             ▼
                          make_manifest.py ──► web/data/manifest.json
                                             │
                                             ▼
                     web/ (static viewer) ──► GitHub Pages
```

Two manually-triggered workflows:

| Workflow | When to run | What it does |
|---|---|---|
| **build-isis-image** | only when `docker/Dockerfile` changes | builds the ISIS container, pushes to GHCR (slow, rare) |
| **process-images** | whenever you want to (re)process frames | pulls the container, runs the pipeline on `frame_count` frames, uploads an artifact, optionally commits `web/data/**` |

Start every `process-images` run with `frame_count = 1` to smoke-test, then scale
to 20.

## Local development

Requires only Python 3.9+ (standard library). No ISIS, no Docker.

```bash
python -m pytest                             # unit tests (no network)
python scripts/download_data.py --limit 2    # download 2 real frames -> data/raw/
python scripts/make_manifest.py \
    --labels data/raw --images web/data/images --out web/data/manifest.json
python -m http.server -d web 8000            # open http://localhost:8000
```

The ISIS step can't run locally; syntax-check and dry-run it instead:

```bash
bash -n scripts/isis_pipeline.sh
bash scripts/isis_pipeline.sh --dry-run data/raw web/data/images
```

## Repository layout

See [`CLAUDE.md`](CLAUDE.md) for the full architecture, the local-vs-CI boundary,
and the project constraints.

## Data source & citation

Hayabusa2 ONC data are archived by the NASA Planetary Data System Small Bodies
Node, bundle `urn:jaxa:darts:hyb2_onc::1.0`:

> Sugita, S., *et al.* (2022). *Hayabusa2 ONC Bundle V1.0*,
> urn:jaxa:darts:hyb2_onc::1.0, NASA Planetary Data System.
> <https://doi.org/10.17597/isas.darts/hyb2-00200>

Image data courtesy JAXA / University of Tokyo and collaborators. See
[`LICENSE`](LICENSE) (MIT for the code).
