#!/usr/bin/env python3
"""Download Hayabusa2 ONC-W2 Earth-flyby source frames from the NASA PDS archive.

Pure Python standard library only — this runs on a bare machine with no installs
and no ISIS/Docker. It fetches, for every frame ID listed in ``config/frames.txt``:

    <id>.fit   raw FITS image (~2 MB)
    <id>.xml   PDS4 label

into ``data/raw/`` (which is git-ignored). Existing, valid files are skipped so
the script is safe to re-run and resume.

Examples
--------
    python scripts/download_data.py --limit 1        # smoke test: first frame only
    python scripts/download_data.py                  # all frames in config/frames.txt
    python scripts/download_data.py --force --limit 3
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Directory in the PDS SBN archive that holds the 2015-12-03 Earth swing-by frames.
DEFAULT_BASE_URL = (
    "https://sbnarchive.psi.edu/pds4/hayabusa2/hyb2_onc/"
    "data_raw/earth_swing-by/20151203/"
)
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRAMES = REPO_ROOT / "config" / "frames.txt"
DEFAULT_DEST = REPO_ROOT / "data" / "raw"

# A real FITS file starts with the 80-byte card ``SIMPLE  =`` ...
FITS_MAGIC = b"SIMPLE  ="
# ... and the smallest plausible label / image we would accept.
MIN_FIT_BYTES = 100_000
MIN_XML_BYTES = 1_000

USER_AGENT = (
    "hayabusa2-onc-pipeline/0.1 "
    "(+https://github.com/freemanjudd/hayabusa2-onc-pipeline; research/education)"
)

log = logging.getLogger("download_data")


def read_frames(path: Path) -> list[str]:
    """Parse a frames file: one frame ID per line, ``#`` comments and blanks ignored."""
    frames: list[str] = []
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        frames.append(line)
    if not frames:
        raise ValueError(f"no frame IDs found in {path}")
    # Guard against accidental duplicates in the source-of-truth file.
    dupes = {f for f in frames if frames.count(f) > 1}
    if dupes:
        raise ValueError(f"duplicate frame IDs in {path}: {sorted(dupes)}")
    return frames


def build_urls(frame_id: str, base_url: str = DEFAULT_BASE_URL) -> dict[str, str]:
    """Map a frame ID to its ``.fit`` and ``.xml`` download URLs."""
    base = base_url if base_url.endswith("/") else base_url + "/"
    return {"fit": f"{base}{frame_id}.fit", "xml": f"{base}{frame_id}.xml"}


def _looks_valid(path: Path, kind: str) -> bool:
    """Cheap sanity check that a previously downloaded file is complete/usable."""
    if not path.is_file():
        return False
    size = path.stat().st_size
    if kind == "fit":
        if size < MIN_FIT_BYTES:
            return False
        with path.open("rb") as fh:
            return fh.read(len(FITS_MAGIC)) == FITS_MAGIC
    if kind == "xml":
        if size < MIN_XML_BYTES:
            return False
        head = path.read_bytes()[:4096]
        return b"<Product_Observational" in head or b"<?xml" in head
    return True


def download_file(
    url: str,
    dest: Path,
    *,
    kind: str,
    force: bool = False,
    retries: int = 3,
    timeout: float = 60.0,
    opener: urllib.request.OpenerDirector | None = None,
) -> str:
    """Download ``url`` to ``dest``. Returns 'skipped', 'downloaded'.

    Raises on failure after exhausting retries or if the result fails validation.
    """
    dest = Path(dest)
    if not force and _looks_valid(dest, kind):
        log.info("skip   %s (already present, %d bytes)", dest.name, dest.stat().st_size)
        return "skipped"

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    _opener = opener or urllib.request.build_opener()

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with _opener.open(req, timeout=timeout) as resp:
                if getattr(resp, "status", 200) not in (200, None):
                    raise urllib.error.HTTPError(
                        url, resp.status, f"HTTP {resp.status}", resp.headers, None
                    )
                data = resp.read()
            tmp.write_bytes(data)
            if not _looks_valid(tmp, kind):
                raise ValueError(
                    f"downloaded {dest.name} failed validation "
                    f"({tmp.stat().st_size} bytes) — archive layout may have changed"
                )
            tmp.replace(dest)
            log.info("ok     %s (%d bytes)", dest.name, dest.stat().st_size)
            return "downloaded"
        except Exception as err:  # noqa: BLE001 - retry any transient failure
            last_err = err
            log.warning("retry  %s attempt %d/%d: %s", dest.name, attempt, retries, err)
            if attempt < retries:
                time.sleep(2 ** attempt)
        finally:
            tmp.unlink(missing_ok=True)

    raise RuntimeError(f"failed to download {url}: {last_err}")


def download_frames(
    frame_ids: list[str],
    dest_dir: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    force: bool = False,
    retries: int = 3,
    opener: urllib.request.OpenerDirector | None = None,
) -> dict[str, int]:
    """Download the ``.fit`` + ``.xml`` for each frame ID. Returns a result tally."""
    dest_dir = Path(dest_dir)
    tally = {"downloaded": 0, "skipped": 0, "failed": 0}
    failures: list[str] = []

    for i, frame_id in enumerate(frame_ids, start=1):
        log.info("[%d/%d] %s", i, len(frame_ids), frame_id)
        urls = build_urls(frame_id, base_url)
        for kind, url in urls.items():
            target = dest_dir / f"{frame_id}.{kind}"
            try:
                result = download_file(
                    url, target, kind=kind, force=force, retries=retries, opener=opener
                )
                tally[result] += 1
            except Exception as err:  # noqa: BLE001
                tally["failed"] += 1
                failures.append(f"{frame_id}.{kind}: {err}")
                log.error("FAIL   %s.%s: %s", frame_id, kind, err)

    if failures:
        log.error("%d download(s) failed:", len(failures))
        for f in failures:
            log.error("  - %s", f)
    return tally


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--frames", type=Path, default=DEFAULT_FRAMES,
                   help=f"frame-list file (default: {DEFAULT_FRAMES})")
    p.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                   help=f"output directory (default: {DEFAULT_DEST})")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="download only the first N frames (mirrors the CI 'test on 1' habit)")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="PDS archive directory URL")
    p.add_argument("--force", action="store_true", help="re-download even if a valid file exists")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    frames = read_frames(args.frames)
    if args.limit is not None:
        if args.limit < 1:
            log.error("--limit must be >= 1")
            return 2
        frames = frames[: args.limit]

    log.info("downloading %d frame(s) -> %s", len(frames), args.dest)
    tally = download_frames(frames, args.dest, base_url=args.base_url, force=args.force)
    log.info(
        "done: %d downloaded, %d skipped, %d failed",
        tally["downloaded"], tally["skipped"], tally["failed"],
    )
    return 1 if tally["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
