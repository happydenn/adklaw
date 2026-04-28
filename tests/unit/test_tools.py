"""Tests for `app.tools` — file IO, shell, web_fetch, and web_search tools."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app import tools
from app.tools import (
    EDIT_DIFF_MAX_BYTES,
    EDIT_SNAPSHOTS_PER_FILE,
    MAX_FETCH_BYTES,
    MAX_GREP_RESULTS,
    edit_file,
    glob_files,
    grep,
    list_dir,
    read_file,
    run_shell,
    undo_last_edit,
    web_fetch,
    web_search,
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
    (workspace_dir / "f.txt").write_text(
        "first line\nalpha beta gamma\nthird line\n", encoding="utf-8"
    )
    read_file("f.txt")
    result = edit_file(
        "f.txt", "alpha beta gamma\nthird line", "alpha BETA gamma\nthird line"
    )
    assert result["status"] == "success"
    assert (workspace_dir / "f.txt").read_text(encoding="utf-8") == (
        "first line\nalpha BETA gamma\nthird line\n"
    )


def test_edit_file_no_match(workspace_dir: Path) -> None:
    (workspace_dir / "f.txt").write_text(
        "hello world\nsecond line\n", encoding="utf-8"
    )
    read_file("f.txt")
    result = edit_file(
        "f.txt", "missing\nstring", "replacement\nstring"
    )
    assert result["status"] == "error"
    assert "not found" in result["error"]


def test_edit_file_multiple_matches(workspace_dir: Path) -> None:
    (workspace_dir / "f.txt").write_text(
        "dup line\nfiller\ndup line\n", encoding="utf-8"
    )
    read_file("f.txt")
    result = edit_file("f.txt", "dup line\n", "x line\n")
    assert result["status"] == "error"
    assert "not unique" in result["error"]


def test_edit_file_missing_file(workspace_dir: Path) -> None:
    result = edit_file(
        "nope.txt", "first line\nsecond line", "first line\nSECOND line"
    )
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# edit_file: read-before-edit invariant
# ---------------------------------------------------------------------------


def test_edit_requires_prior_read(workspace_dir: Path) -> None:
    (workspace_dir / "f.txt").write_text(
        "first line\nsecond line\n", encoding="utf-8"
    )
    # No read_file call — cache miss.
    result = edit_file(
        "f.txt", "first line\nsecond line", "first line\nSECOND line"
    )
    assert result["status"] == "error"
    assert "read the file first" in result["error"]


def test_edit_after_read_succeeds(workspace_dir: Path, state_dir: Path) -> None:
    (workspace_dir / "f.txt").write_text(
        "first line\nsecond line\n", encoding="utf-8"
    )
    read_file("f.txt")
    result = edit_file(
        "f.txt", "first line\nsecond line", "first line\nSECOND line"
    )
    assert result["status"] == "success"


def test_edit_after_external_change_errors(
    workspace_dir: Path, state_dir: Path
) -> None:
    target = workspace_dir / "f.txt"
    target.write_text("first line\nsecond line\n", encoding="utf-8")
    read_file("f.txt")
    # Mimic an external write (e.g. user edited the file in their editor).
    target.write_text("first line\nNEW line\n", encoding="utf-8")
    result = edit_file(
        "f.txt", "first line\nNEW line", "first line\nNEWER line"
    )
    assert result["status"] == "error"
    assert "changed since last read" in result["error"]


def test_successful_edit_updates_read_cache(
    workspace_dir: Path, state_dir: Path
) -> None:
    """One read should be enough for a sequence of successful edits;
    the cache is refreshed on each successful edit."""
    (workspace_dir / "f.txt").write_text(
        "alpha line\nbeta line\ngamma line\n", encoding="utf-8"
    )
    read_file("f.txt")
    r1 = edit_file("f.txt", "alpha line\nbeta", "ALPHA line\nbeta")
    assert r1["status"] == "success"
    r2 = edit_file("f.txt", "ALPHA line\nbeta", "ALPHA line\nBETA")
    assert r2["status"] == "success"


# ---------------------------------------------------------------------------
# edit_file: anchor minimum
# ---------------------------------------------------------------------------


def test_short_unanchored_old_string_rejected(workspace_dir: Path) -> None:
    (workspace_dir / "f.txt").write_text("alpha beta gamma\n", encoding="utf-8")
    read_file("f.txt")
    result = edit_file("f.txt", "beta", "BETA")
    assert result["status"] == "error"
    assert "anchor" in result["error"]


def test_short_old_string_with_newline_allowed(
    workspace_dir: Path, state_dir: Path
) -> None:
    (workspace_dir / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    read_file("f.txt")
    # Short but multi-line — newline counts as anchor.
    result = edit_file("f.txt", "a\nb", "a\nB")
    assert result["status"] == "success"


# ---------------------------------------------------------------------------
# edit_file: net-deletion guard
# ---------------------------------------------------------------------------


def test_large_byte_deletion_rejected_without_flag(
    workspace_dir: Path, state_dir: Path
) -> None:
    """Edit removing > 30% of file bytes is refused without the flag."""
    body = "header\n" + ("x" * 1000) + "\nfooter\n"
    (workspace_dir / "f.txt").write_text(body, encoding="utf-8")
    read_file("f.txt")
    result = edit_file(
        "f.txt", "header\n" + ("x" * 1000) + "\nfooter", "header\nfooter"
    )
    assert result["status"] == "error"
    assert "remove" in result["error"]
    assert "allow_large_deletion" in result["error"]


def test_large_line_deletion_rejected_without_flag(
    workspace_dir: Path, state_dir: Path
) -> None:
    """Edit removing >= 40 lines is refused without the flag."""
    lines = "\n".join(f"line {i}" for i in range(60)) + "\n"
    (workspace_dir / "f.txt").write_text(lines, encoding="utf-8")
    read_file("f.txt")
    block = "\n".join(f"line {i}" for i in range(5, 55))
    result = edit_file("f.txt", block, "line 5")
    assert result["status"] == "error"
    assert "lines" in result["error"]


def test_large_deletion_allowed_with_flag(
    workspace_dir: Path, state_dir: Path
) -> None:
    body = "header\n" + ("x" * 1000) + "\nfooter\n"
    (workspace_dir / "f.txt").write_text(body, encoding="utf-8")
    read_file("f.txt")
    result = edit_file(
        "f.txt",
        "header\n" + ("x" * 1000) + "\nfooter",
        "header\nfooter",
        allow_large_deletion=True,
    )
    assert result["status"] == "success"
    assert "diff" in result


# ---------------------------------------------------------------------------
# edit_file: unified diff in response
# ---------------------------------------------------------------------------


def test_edit_returns_unified_diff(
    workspace_dir: Path, state_dir: Path
) -> None:
    (workspace_dir / "f.txt").write_text(
        "alpha line\nbeta line\n", encoding="utf-8"
    )
    read_file("f.txt")
    result = edit_file("f.txt", "alpha line\nbeta", "alpha line\nBETA")
    assert result["status"] == "success"
    diff = result["diff"]
    assert "--- a/f.txt" in diff
    assert "+++ b/f.txt" in diff
    assert "+alpha line" not in diff  # context line, not added
    assert "+alpha line\n+BETA" in diff or "+BETA" in diff


def test_diff_truncated_when_huge(
    workspace_dir: Path, state_dir: Path
) -> None:
    """A diff bigger than EDIT_DIFF_MAX_BYTES is truncated with a marker."""
    # File of distinct lines so the diff includes them all.
    body = "".join(f"old-line-{i:04d}\n" for i in range(2000))
    (workspace_dir / "f.txt").write_text(body, encoding="utf-8")
    read_file("f.txt")
    new_body = "".join(f"new-line-{i:04d}\n" for i in range(2000))
    result = edit_file(
        "f.txt", body, new_body, allow_large_deletion=True
    )
    assert result["status"] == "success"
    diff = result["diff"]
    assert "(diff truncated)" in diff
    assert len(diff.encode("utf-8")) <= EDIT_DIFF_MAX_BYTES + 64


# ---------------------------------------------------------------------------
# edit_file: snapshot + undo_last_edit
# ---------------------------------------------------------------------------


def test_edit_writes_snapshot_under_state_dir(
    workspace_dir: Path, state_dir: Path
) -> None:
    (workspace_dir / "f.txt").write_text(
        "alpha line\nbeta line\n", encoding="utf-8"
    )
    read_file("f.txt")
    result = edit_file("f.txt", "alpha line\nbeta", "alpha line\nBETA")
    assert result["status"] == "success"
    snap_path = Path(result["snapshot"])
    assert snap_path.exists()
    assert snap_path.is_relative_to(state_dir)
    assert snap_path.read_text(encoding="utf-8") == "alpha line\nbeta line\n"


def test_undo_restores_previous_content(
    workspace_dir: Path, state_dir: Path
) -> None:
    target = workspace_dir / "f.txt"
    target.write_text("alpha line\nbeta line\n", encoding="utf-8")
    read_file("f.txt")
    edit_result = edit_file("f.txt", "alpha line\nbeta", "alpha line\nBETA")
    assert edit_result["status"] == "success"
    snap_path = Path(edit_result["snapshot"])

    undo_result = undo_last_edit("f.txt")
    assert undo_result["status"] == "success"
    assert target.read_text(encoding="utf-8") == "alpha line\nbeta line\n"
    assert not snap_path.exists()


def test_undo_with_no_snapshots_errors(
    workspace_dir: Path, state_dir: Path
) -> None:
    (workspace_dir / "f.txt").write_text("hi\n", encoding="utf-8")
    result = undo_last_edit("f.txt")
    assert result["status"] == "error"
    assert "No snapshot" in result["error"]


def test_snapshot_retention_caps_per_file(
    workspace_dir: Path, state_dir: Path
) -> None:
    """After more edits than the retention cap, only the newest cap-many
    snapshot files for that path remain."""
    target = workspace_dir / "f.txt"
    target.write_text("v0 line\nbody\n", encoding="utf-8")
    read_file("f.txt")
    n = EDIT_SNAPSHOTS_PER_FILE + 5
    for i in range(n):
        prev = f"v{i} line"
        new = f"v{i + 1} line"
        # Sleep 1ms between edits so timestamps differ.
        time.sleep(0.002)
        result = edit_file("f.txt", f"{prev}\nbody", f"{new}\nbody")
        assert result["status"] == "success", result
    from app.tools import _path_hash, _snapshot_dir

    sha_prefix = _path_hash(target.resolve())
    snaps = list(_snapshot_dir().glob(f"{sha_prefix}-*"))
    assert len(snaps) == EDIT_SNAPSHOTS_PER_FILE


def test_atomic_write_does_not_leave_tempfile(
    workspace_dir: Path, state_dir: Path
) -> None:
    (workspace_dir / "f.txt").write_text(
        "alpha line\nbeta line\n", encoding="utf-8"
    )
    read_file("f.txt")
    result = edit_file("f.txt", "alpha line\nbeta", "alpha line\nBETA")
    assert result["status"] == "success"
    leftover = list(workspace_dir.glob(".adklaw-*"))
    assert leftover == []


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


# ---------------------------------------------------------------------------
# web_search — Gemini Flash-Lite + Google Search grounding
# ---------------------------------------------------------------------------


def _fake_response(
    *,
    text: str,
    sources: list[tuple[str, str]] | None = None,
    queries: list[str] | None = None,
    has_grounding: bool = True,
) -> Any:
    """Build a SimpleNamespace shaped like a `genai` response object."""
    if has_grounding:
        chunks = [
            SimpleNamespace(web=SimpleNamespace(uri=url, title=title))
            for title, url in (sources or [])
        ]
        gm = SimpleNamespace(
            grounding_chunks=chunks,
            web_search_queries=queries or [],
        )
    else:
        gm = None
    candidate = SimpleNamespace(grounding_metadata=gm)
    return SimpleNamespace(text=text, candidates=[candidate])


class _RecordingClient:
    """Stand-in for `genai.Client` that records the last `generate_content`
    call kwargs and returns a fixed response."""

    def __init__(self, response: Any | Exception) -> None:
        self._response = response
        self.last_kwargs: dict[str, Any] | None = None
        self.models = SimpleNamespace(generate_content=self._generate_content)

    def _generate_content(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture
def _patch_client(monkeypatch: pytest.MonkeyPatch):
    """Patch `_web_search_client` to return a `_RecordingClient` we control."""

    def _factory(response: Any | Exception) -> _RecordingClient:
        client = _RecordingClient(response)
        monkeypatch.setattr(tools, "_web_search_client", lambda: client)
        return client

    return _factory


def test_web_search_happy_with_grounding(_patch_client) -> None:
    client = _patch_client(
        _fake_response(
            text="The capital is Taipei.",
            sources=[
                ("Wikipedia: Taipei", "https://en.wikipedia.org/wiki/Taipei"),
                ("CIA Factbook: Taiwan", "https://www.cia.gov/the-world-factbook/countries/taiwan/"),
            ],
            queries=["capital of taiwan"],
        )
    )
    result = web_search("What is the capital of Taiwan?")
    assert result["status"] == "success"
    assert result["answer"] == "The capital is Taipei."
    assert len(result["sources"]) == 2
    assert result["sources"][0]["url"].startswith("https://en.wikipedia.org")
    assert result["sources"][0]["title"] == "Wikipedia: Taipei"
    assert result["search_queries"] == ["capital of taiwan"]
    assert client.last_kwargs is not None


def test_web_search_happy_no_grounding(_patch_client) -> None:
    _patch_client(
        _fake_response(text="Paris.", has_grounding=False)
    )
    result = web_search("capital of france?")
    assert result["status"] == "success"
    assert result["answer"] == "Paris."
    assert result["sources"] == []
    assert result["search_queries"] == []


def test_web_search_dedupes_duplicate_urls(_patch_client) -> None:
    _patch_client(
        _fake_response(
            text="Yes.",
            sources=[
                ("First", "https://example.com/x"),
                ("Second", "https://example.com/x"),
                ("Third", "https://example.com/y"),
            ],
        )
    )
    result = web_search("anything")
    urls = [s["url"] for s in result["sources"]]
    assert urls == ["https://example.com/x", "https://example.com/y"]


def test_web_search_empty_query_errors(_patch_client) -> None:
    client = _patch_client(_fake_response(text="should not run"))
    result = web_search("   ")
    assert result["status"] == "error"
    assert "non-empty" in result["error"]
    assert client.last_kwargs is None  # client never called


def test_web_search_blocked_response_errors(_patch_client) -> None:
    _patch_client(_fake_response(text="", has_grounding=False))
    result = web_search("anything")
    assert result["status"] == "error"
    assert "no text" in result["error"].lower()


def test_web_search_sdk_raises_returns_error(_patch_client) -> None:
    _patch_client(RuntimeError("vertex on fire"))
    result = web_search("anything")
    assert result["status"] == "error"
    assert "vertex on fire" in result["error"]


def test_web_search_uses_env_model_override(
    _patch_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADKLAW_WEB_SEARCH_MODEL", "alt-model")
    client = _patch_client(_fake_response(text="ok"))
    web_search("anything")
    assert client.last_kwargs["model"] == "alt-model"


def test_web_search_default_latlng_is_taipei(_patch_client) -> None:
    client = _patch_client(_fake_response(text="ok"))
    web_search("anything")
    config = client.last_kwargs["config"]
    tool_config = config.tool_config
    lat_lng = tool_config.retrieval_config.lat_lng
    assert abs(lat_lng.latitude - 25.0330) < 1e-6
    assert abs(lat_lng.longitude - 121.5654) < 1e-6


def test_web_search_latlng_override(
    _patch_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADKLAW_WEB_SEARCH_LATLNG", "37.4220,-122.0841")
    client = _patch_client(_fake_response(text="ok"))
    web_search("anything")
    lat_lng = client.last_kwargs["config"].tool_config.retrieval_config.lat_lng
    assert abs(lat_lng.latitude - 37.4220) < 1e-6
    assert abs(lat_lng.longitude - (-122.0841)) < 1e-6


def test_web_search_latlng_empty_disables_bias(
    _patch_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADKLAW_WEB_SEARCH_LATLNG", "")
    client = _patch_client(_fake_response(text="ok"))
    web_search("anything")
    config = client.last_kwargs["config"]
    assert config.tool_config is None


def test_web_search_latlng_invalid_falls_through(
    _patch_client,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    monkeypatch.setenv("ADKLAW_WEB_SEARCH_LATLNG", "not-a-coord")
    client = _patch_client(_fake_response(text="ok"))
    with caplog.at_level(logging.WARNING, logger="app.tools"):
        result = web_search("anything")
    assert result["status"] == "success"
    config = client.last_kwargs["config"]
    assert config.tool_config is None
    assert any("not-a-coord" in r.message for r in caplog.records)
