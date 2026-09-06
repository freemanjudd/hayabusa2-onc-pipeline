"""Tests for scripts/download_data.py.

Network is faked with a threaded ``http.server`` bound to localhost, so these
run offline and fast.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import download_data
import pytest

# A minimal but valid-looking FITS payload: the magic card, padded past the
# MIN_FIT_BYTES floor the validator enforces.
FAKE_FIT = download_data.FITS_MAGIC + b" " * download_data.MIN_FIT_BYTES
FAKE_XML = (
    b'<?xml version="1.0"?>\n<Product_Observational>'
    + b"x" * 2000
    + b"</Product_Observational>"
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        if self.path.endswith(".fit"):
            body = FAKE_FIT
        elif self.path.endswith(".xml"):
            body = FAKE_XML
        else:
            self.send_error(404)
            return
        if "missing" in self.path:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address
    yield f"http://{host}:{port}/frames/"
    httpd.shutdown()


# --- read_frames -----------------------------------------------------------

def test_read_frames_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "frames.txt"
    f.write_text("# header\n\nframe_a\n  frame_b  \n\n# trailing\n")
    assert download_data.read_frames(f) == ["frame_a", "frame_b"]


def test_read_frames_rejects_duplicates(tmp_path):
    f = tmp_path / "frames.txt"
    f.write_text("frame_a\nframe_b\nframe_a\n")
    with pytest.raises(ValueError, match="duplicate"):
        download_data.read_frames(f)


def test_read_frames_rejects_empty(tmp_path):
    f = tmp_path / "frames.txt"
    f.write_text("# only comments\n")
    with pytest.raises(ValueError):
        download_data.read_frames(f)


def test_repo_frames_file_is_the_approach_sequence():
    frames = download_data.read_frames(download_data.DEFAULT_FRAMES)
    assert len(frames) == 19
    assert frames == sorted(frames)  # chronological
    assert all(f.startswith("hyb2_onc_20151203_") and f.endswith("_w2f_l2a") for f in frames)


# --- build_urls ------------------------------------------------------------

def test_build_urls_joins_cleanly():
    urls = download_data.build_urls("abc", "http://x/dir")  # no trailing slash
    assert urls == {"fit": "http://x/dir/abc.fit", "xml": "http://x/dir/abc.xml"}


# --- download_file / download_frames -------------------------------------

def test_download_file_fetches_and_validates(server, tmp_path):
    dest = tmp_path / "abc.fit"
    result = download_data.download_file(server + "abc.fit", dest, kind="fit")
    assert result == "downloaded"
    assert dest.read_bytes().startswith(download_data.FITS_MAGIC)


def test_download_file_skips_existing_valid(server, tmp_path):
    dest = tmp_path / "abc.fit"
    dest.write_bytes(FAKE_FIT)
    result = download_data.download_file(server + "abc.fit", dest, kind="fit")
    assert result == "skipped"


def test_download_file_force_redownloads(server, tmp_path):
    dest = tmp_path / "abc.fit"
    dest.write_bytes(FAKE_FIT)
    result = download_data.download_file(server + "abc.fit", dest, kind="fit", force=True)
    assert result == "downloaded"


def test_download_file_rejects_too_small(server, tmp_path):
    # .xml route returns a >MIN_XML payload; ask for it as 'fit' so the size floor trips.
    dest = tmp_path / "abc.fit"
    with pytest.raises(RuntimeError):
        download_data.download_file(server + "abc.xml", dest, kind="fit", retries=1)
    assert not dest.exists()


def test_download_frames_reports_failures(server, tmp_path):
    tally = download_data.download_frames(
        ["good_frame", "missing_frame"], tmp_path, base_url=server, retries=1,
    )
    # good_frame: 2 files ok; missing_frame: 2 files 404
    assert tally["downloaded"] == 2
    assert tally["failed"] == 2


def test_main_limit_and_exit_code(server, tmp_path, monkeypatch):
    frames = tmp_path / "frames.txt"
    frames.write_text("good_a\ngood_b\ngood_c\n")
    rc = download_data.main(
        ["--frames", str(frames), "--dest", str(tmp_path / "out"),
         "--base-url", server, "--limit", "2"]
    )
    assert rc == 0
    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == [
        "good_a.fit", "good_a.xml", "good_b.fit", "good_b.xml",
    ]


def test_main_rejects_bad_limit(tmp_path):
    frames = tmp_path / "frames.txt"
    frames.write_text("good_a\n")
    assert download_data.main(["--frames", str(frames), "--limit", "0"]) == 2
