"""Tests for `app.tools` — file IO, shell, and web_fetch tools."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app import tools
from app.tools import (
    MAX_FETCH_BYTES,
    MAX_GREP_RESULTS,
    edit_file,
    glob_files,
    grep,
    list_dir,
    read_file,
    run_shell,
    web_fetch,
    write_file,
)

# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def test_read_file_happy(workspace_dir: Path) -> None:
    (workspace_dir / "hello.txt").write_text("hi there", encoding="utf-8")
    result = read_file("hello.txt")
    assert result["status"] == "success"
    assert result["content"] == "hi there"


def test_read_file_missing(workspace_dir: Path) -> None:
    result = read_file("nope.txt")
    assert result["status"] == "error"
    assert "does not exist" in result["error"]


def test_read_file_outside_workspace(workspace_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    result = read_file(str(outside))
    assert result["status"] == "error"


def test_read_file_too_large(workspace_dir: Path) -> None:
    big = workspace_dir / "big.txt"
    big.write_bytes(b"x" * (tools.MAX_FILE_BYTES + 1))
    result = read_file("big.txt")
    assert result["status"] == "error"
    assert "too large" in result["error"]


def test_read_file_non_utf8(workspace_dir: Path) -> None:
    (workspace_dir / "binary.bin").write_bytes(b"\xff\xfe\x00\x01")
    result = read_file("binary.bin")
    assert result["status"] == "error"
    assert "UTF-8" in result["error"]


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


def test_write_file_happy(workspace_dir: Path) -> None:
    result = write_file("note.txt", "hello")
    assert result["status"] == "success"
    assert (workspace_dir / "note.txt").read_text(encoding="utf-8") == "hello"
    assert result["bytes_written"] == 5


def test_write_file_creates_parents(workspace_dir: Path) -> None:
    result = write_file("nested/deep/note.txt", "x")
    assert result["status"] == "success"
    assert (workspace_dir / "nested" / "deep" / "note.txt").is_file()


def test_write_file_outside_workspace(workspace_dir: Path) -> None:
    result = write_file("../escape.txt", "x")
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


def test_edit_file_unique_match(workspace_dir: Path) -> None:
    (workspace_dir / "f.txt").write_text("alpha beta gamma", encoding="utf-8")
    result = edit_file("f.txt", "beta", "BETA")
    assert result["status"] == "success"
    assert (workspace_dir / "f.txt").read_text(encoding="utf-8") == "alpha BETA gamma"


def test_edit_file_no_match(workspace_dir: Path) -> None:
    (workspace_dir / "f.txt").write_text("hello", encoding="utf-8")
    result = edit_file("f.txt", "missing", "x")
    assert result["status"] == "error"
    assert "not found" in result["error"]


def test_edit_file_multiple_matches(workspace_dir: Path) -> None:
    (workspace_dir / "f.txt").write_text("dup dup", encoding="utf-8")
    result = edit_file("f.txt", "dup", "x")
    assert result["status"] == "error"
    assert "not unique" in result["error"]


def test_edit_file_missing_file(workspace_dir: Path) -> None:
    result = edit_file("nope.txt", "a", "b")
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


def test_list_dir_happy(workspace_dir: Path) -> None:
    (workspace_dir / "a.txt").write_text("", encoding="utf-8")
    (workspace_dir / "subdir").mkdir()
    result = list_dir(".")
    assert result["status"] == "success"
    by_name = {e["name"]: e["type"] for e in result["entries"]}
    assert by_name == {"a.txt": "file", "subdir": "dir"}


def test_list_dir_missing(workspace_dir: Path) -> None:
    result = list_dir("nope")
    assert result["status"] == "error"
    assert "does not exist" in result["error"]


def test_list_dir_not_a_directory(workspace_dir: Path) -> None:
    (workspace_dir / "f.txt").write_text("", encoding="utf-8")
    result = list_dir("f.txt")
    assert result["status"] == "error"
    assert "Not a directory" in result["error"]


# ---------------------------------------------------------------------------
# glob_files
# ---------------------------------------------------------------------------


def test_glob_files_happy(workspace_dir: Path) -> None:
    (workspace_dir / "a.py").write_text("", encoding="utf-8")
    nested = workspace_dir / "pkg"
    nested.mkdir()
    (nested / "b.py").write_text("", encoding="utf-8")
    (workspace_dir / "c.txt").write_text("", encoding="utf-8")
    result = glob_files("**/*.py")
    assert result["status"] == "success"
    assert set(result["matches"]) == {"a.py", "pkg/b.py"}


def test_glob_files_no_matches(workspace_dir: Path) -> None:
    result = glob_files("**/*.never")
    assert result["status"] == "success"
    assert result["matches"] == []


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def test_grep_finds_matches(workspace_dir: Path) -> None:
    (workspace_dir / "a.txt").write_text(
        "first\nfindme here\nthird\n", encoding="utf-8"
    )
    result = grep("findme")
    assert result["status"] == "success"
    assert len(result["matches"]) == 1
    m = result["matches"][0]
    assert m["path"] == "a.txt"
    assert m["line"] == 2
    assert "findme" in m["text"]


def test_grep_truncates_at_limit(workspace_dir: Path) -> None:
    body = "\n".join(f"hit {i}" for i in range(MAX_GREP_RESULTS + 50)) + "\n"
    (workspace_dir / "a.txt").write_text(body, encoding="utf-8")
    result = grep("hit")
    assert result["status"] == "success"
    assert result["truncated"] is True
    assert len(result["matches"]) == MAX_GREP_RESULTS


def test_grep_invalid_regex(workspace_dir: Path) -> None:
    result = grep("(unclosed")
    assert result["status"] == "error"
    assert "regex" in result["error"].lower()


# ---------------------------------------------------------------------------
# run_shell
# ---------------------------------------------------------------------------


def test_run_shell_echo(workspace_dir: Path) -> None:
    result = run_shell("echo hi")
    assert result["status"] == "success"
    assert result["exit_code"] == 0
    assert result["stdout"] == "hi\n"


def test_run_shell_cwd_is_workspace(workspace_dir: Path) -> None:
    result = run_shell("pwd")
    assert result["status"] == "success"
    assert result["stdout"].strip() == str(workspace_dir)


def test_run_shell_timeout(
    workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "SHELL_TIMEOUT_SECONDS", 1)
    result = run_shell("sleep 5")
    assert result["status"] == "error"
    assert "timed out" in result["error"]


# ---------------------------------------------------------------------------
# web_fetch — uses an in-process http.server, never touches the network
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    body: bytes = b"hello world"
    content_type: str = "text/plain"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: object) -> None:
        return  # silence test logs


@pytest.fixture
def stub_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_fetch_happy(stub_server: str) -> None:
    _StubHandler.body = b"hello world"
    _StubHandler.content_type = "text/plain"
    result = web_fetch(stub_server)
    assert result["status"] == "success"
    assert result["status_code"] == 200
    assert result["text"] == "hello world"
    assert result["truncated"] is False


def test_web_fetch_rejects_non_http() -> None:
    result = web_fetch("ftp://example.com/")
    assert result["status"] == "error"
    assert "http" in result["error"].lower()


def test_web_fetch_truncates(stub_server: str) -> None:
    _StubHandler.body = b"a" * (MAX_FETCH_BYTES + 1000)
    _StubHandler.content_type = "text/plain"
    result = web_fetch(stub_server)
    assert result["status"] == "success"
    assert result["truncated"] is True
    assert len(result["text"]) <= MAX_FETCH_BYTES
