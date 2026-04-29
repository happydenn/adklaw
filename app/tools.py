"""Common tools for the adklaw assistant.

All filesystem tools are rooted at the configured workspace and reject paths
that would escape it. `run_shell` executes with the workspace as cwd. All
tools return plain dicts so the LLM gets structured feedback on success or
failure without raising exceptions.
"""

from __future__ import annotations

import difflib
import functools
import hashlib
import logging
import mimetypes
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from google import genai
from google.adk.tools import ToolContext
from google.genai import types

from .state import get_state_dir
from .workspace import get_workspace, resolve_in_workspace

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_000_000  # 1 MB read cap
MAX_GREP_RESULTS = 200
MAX_FETCH_BYTES = 2_000_000  # 2 MB fetch cap
SHELL_TIMEOUT_SECONDS = 60
DEFAULT_SEND_FILE_MAX_BYTES = 25_000_000

WEB_SEARCH_MODEL_DEFAULT = "gemini-2.5-flash-lite"
WEB_SEARCH_LATLNG_DEFAULT = "25.0330,121.5654"  # Taipei

# `edit_file` safety knobs.
EDIT_DELETE_RATIO_THRESHOLD = 0.30
EDIT_DELETE_LINES_THRESHOLD = 40
EDIT_ANCHOR_MIN_LEN = 20
EDIT_DIFF_MAX_BYTES = 8192
EDIT_SNAPSHOTS_PER_FILE = 20

# Per-process map: resolved absolute path → SHA-256 of contents at last
# `read_file`. `edit_file` requires a recent matching read so a stale
# `old_string` can't clobber concurrent on-disk changes.
_file_read_cache: dict[str, str] = {}


def _ok(**fields) -> dict:
    return {"status": "success", **fields}


def _err(message: str) -> dict:
    return {"status": "error", "error": message}


def _compute_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path_hash(target: Path) -> str:
    """Stable short hash of an absolute path, used to namespace
    snapshot files per logical file."""
    return hashlib.sha1(str(target).encode("utf-8")).hexdigest()[:12]


def _make_diff(before: str, after: str, path: str) -> str:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    if len(diff.encode("utf-8")) > EDIT_DIFF_MAX_BYTES:
        return diff[:EDIT_DIFF_MAX_BYTES] + "\n... (diff truncated)\n"
    return diff


def _snapshot_dir() -> Path:
    d = get_state_dir() / "edits"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_snapshot(target: Path, content: bytes) -> Path:
    """Copy pre-edit bytes into the snapshot dir under a per-path
    prefix, then evict oldest snapshots so each file keeps at most
    `EDIT_SNAPSHOTS_PER_FILE` versions."""
    sha_prefix = _path_hash(target)
    snap = _snapshot_dir() / f"{sha_prefix}-{target.name}.{int(time.time() * 1000)}"
    snap.write_bytes(content)
    siblings = sorted(
        _snapshot_dir().glob(f"{sha_prefix}-*"),
        key=lambda p: p.stat().st_mtime,
    )
    for old in siblings[:-EDIT_SNAPSHOTS_PER_FILE]:
        old.unlink(missing_ok=True)
    return snap


def _atomic_write(target: Path, text: str) -> None:
    """Write `text` to `target` via a sibling tempfile + `os.replace`.

    Crash-safe: a partial write can never leave the target half-
    written, since the rename is atomic on POSIX and Windows.
    """
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".adklaw-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_file(path: str) -> dict:
    """Read a text file from the workspace.

    Also records the file's SHA-256 in the per-process read cache so
    `edit_file` can verify the content hasn't changed since the agent
    last saw it. Without this guard a stale `old_string` could clobber
    concurrent on-disk changes.

    Args:
        path: File path, relative to the workspace or an absolute path inside
            it.

    Returns:
        On success: {"status": "success", "path": str, "content": str}.
        On error: {"status": "error", "error": str}.
    """
    try:
        target = resolve_in_workspace(path)
    except ValueError as e:
        return _err(str(e))
    if not target.exists():
        return _err(f"File does not exist: {path}")
    if not target.is_file():
        return _err(f"Not a file: {path}")
    try:
        data = target.read_bytes()
    except OSError as e:
        return _err(f"Read failed: {e}")
    if len(data) > MAX_FILE_BYTES:
        return _err(
            f"File too large ({len(data)} bytes > {MAX_FILE_BYTES}). "
            "Use grep or read a smaller slice."
        )
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return _err("File is not valid UTF-8 text.")
    _file_read_cache[str(target)] = _compute_sha(data)
    return _ok(path=str(target), content=content)


def write_file(path: str, content: str) -> dict:
    """Create or overwrite a text file inside the workspace.

    Args:
        path: Destination path inside the workspace. Parent directories are
            created automatically.
        content: UTF-8 text to write.

    Returns:
        {"status": "success", "path": str, "bytes_written": int} or an error.
    """
    try:
        target = resolve_in_workspace(path)
    except ValueError as e:
        return _err(str(e))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        return _err(f"Write failed: {e}")
    return _ok(path=str(target), bytes_written=len(content.encode("utf-8")))


def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    allow_large_deletion: bool = False,
) -> dict:
    """Replace exactly one occurrence of `old_string` with `new_string`.

    Layered safety checks (each catches a different failure mode seen
    in real use):

    1. **Anchor minimum** — `old_string` must be at least 20 chars or
       contain a newline, so a vague short match can't pick the wrong
       location.
    2. **Read-before-edit** — the file must have been read via
       `read_file` and not changed on disk since. Errors with a
       concrete next action ("read first" / "re-read") instead of
       silently clobbering newer content.
    3. **Net-deletion guard** — if the edit would shrink the file by
       ≥30% of bytes or ≥40 lines, refuse unless
       `allow_large_deletion=True`.
    4. **Snapshot before write** — the prior bytes are copied into the
       state dir so `undo_last_edit(path)` can roll back.
    5. **Atomic write** — a sibling tempfile + `os.replace` so a
       crash mid-write can't truncate the target.

    On success, the response includes a unified diff so the agent can
    self-check what landed.

    Args:
        path: Existing file inside the workspace.
        old_string: Exact string to find. Must occur exactly once.
        new_string: Replacement string.
        allow_large_deletion: Bypass the net-deletion guard. Use only
            when the deletion is genuinely intended.

    Returns:
        On success: `{"status": "success", "path", "diff", "snapshot"}`.
        On any safety violation or IO failure: an error dict.
    """
    try:
        target = resolve_in_workspace(path)
    except ValueError as e:
        return _err(str(e))
    if not target.is_file():
        return _err(f"Not a file: {path}")

    if len(old_string) < EDIT_ANCHOR_MIN_LEN and "\n" not in old_string:
        return _err(
            "old_string is too short to safely anchor; include at least "
            "2 lines of surrounding context (or a string of at least "
            f"{EDIT_ANCHOR_MIN_LEN} characters)."
        )

    try:
        original_bytes = target.read_bytes()
        original = original_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _err(f"Read failed: {e}")

    cached_sha = _file_read_cache.get(str(target))
    current_sha = _compute_sha(original_bytes)
    if cached_sha is None:
        return _err(
            "edit_file: read the file first with read_file so I can "
            "verify it didn't change out from under you."
        )
    if cached_sha != current_sha:
        return _err(
            "edit_file: file changed since last read; re-read with "
            "read_file before editing."
        )

    occurrences = original.count(old_string)
    if occurrences == 0:
        return _err("old_string not found in file.")
    if occurrences > 1:
        return _err(
            f"old_string is not unique ({occurrences} matches). "
            "Provide more surrounding context."
        )

    updated = original.replace(old_string, new_string, 1)

    deleted_bytes = max(0, len(original) - len(updated))
    deleted_lines = max(0, original.count("\n") - updated.count("\n"))
    if not allow_large_deletion and (
        (deleted_bytes / max(1, len(original))) >= EDIT_DELETE_RATIO_THRESHOLD
        or deleted_lines >= EDIT_DELETE_LINES_THRESHOLD
    ):
        ratio = deleted_bytes / max(1, len(original))
        return _err(
            f"edit_file: this edit would remove {deleted_bytes} bytes "
            f"({ratio:.0%} of file) / {deleted_lines} lines. If "
            "intended, pass allow_large_deletion=True."
        )

    snap = _save_snapshot(target, original_bytes)
    try:
        _atomic_write(target, updated)
    except OSError as e:
        return _err(f"Write failed: {e}")

    _file_read_cache[str(target)] = _compute_sha(updated.encode("utf-8"))
    return _ok(
        path=str(target),
        diff=_make_diff(original, updated, path),
        snapshot=str(snap),
    )


def undo_last_edit(path: str) -> dict:
    """Restore a file from its most recent pre-edit snapshot.

    Use this immediately after an `edit_file` you realised was wrong.
    The matching snapshot is removed once restored, so calling this
    repeatedly walks backward through the snapshot history (one step
    per call) until none remain.

    Args:
        path: Workspace path that was previously edited.

    Returns:
        On success: `{"status": "success", "path", "restored_from"}`.
        On error: `{"status": "error", ...}` (e.g. no snapshot found).
    """
    try:
        target = resolve_in_workspace(path)
    except ValueError as e:
        return _err(str(e))
    sha_prefix = _path_hash(target)
    snaps = sorted(
        _snapshot_dir().glob(f"{sha_prefix}-*"),
        key=lambda p: p.stat().st_mtime,
    )
    if not snaps:
        return _err(f"No snapshot found for {path}.")
    latest = snaps[-1]
    try:
        prior = latest.read_bytes()
        _atomic_write(target, prior.decode("utf-8"))
        latest.unlink()
    except (OSError, UnicodeDecodeError) as e:
        return _err(f"Restore failed: {e}")
    _file_read_cache[str(target)] = _compute_sha(prior)
    return _ok(path=str(target), restored_from=str(latest))


def list_dir(path: str = ".") -> dict:
    """List entries in a workspace directory.

    Args:
        path: Directory inside the workspace. Defaults to the workspace root.

    Returns:
        {"status": "success", "path": str, "entries": [{"name", "type"}...]}
        where type is "file", "dir", or "other".
    """
    try:
        target = resolve_in_workspace(path)
    except ValueError as e:
        return _err(str(e))
    if not target.exists():
        return _err(f"Directory does not exist: {path}")
    if not target.is_dir():
        return _err(f"Not a directory: {path}")
    entries = []
    for child in sorted(target.iterdir()):
        if child.is_dir():
            kind = "dir"
        elif child.is_file():
            kind = "file"
        else:
            kind = "other"
        entries.append({"name": child.name, "type": kind})
    return _ok(path=str(target), entries=entries)


def glob_files(pattern: str) -> dict:
    """Match files in the workspace against a glob pattern.

    Args:
        pattern: Glob pattern, e.g. `**/*.py` or `notes/*.md`. Evaluated
            relative to the workspace root.

    Returns:
        {"status": "success", "matches": [str, ...]} with paths relative to
        the workspace root.
    """
    workspace = get_workspace()
    try:
        matches = sorted(str(p.relative_to(workspace)) for p in workspace.glob(pattern))
    except (ValueError, OSError) as e:
        return _err(f"Glob failed: {e}")
    return _ok(matches=matches)


def grep(pattern: str, path: str = ".", file_glob: str = "**/*") -> dict:
    """Search files in the workspace for a regex pattern.

    Args:
        pattern: Python regex pattern.
        path: Directory inside the workspace to search. Defaults to the root.
        file_glob: Glob restricting which files to scan. Defaults to all files.

    Returns:
        {"status": "success", "matches": [{"path", "line", "text"}, ...]} with
        up to 200 hits.
    """
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return _err(f"Invalid regex: {e}")
    try:
        target = resolve_in_workspace(path)
    except ValueError as e:
        return _err(str(e))
    if not target.is_dir():
        return _err(f"Not a directory: {path}")

    workspace = get_workspace()
    matches = []
    for file in target.glob(file_glob):
        if not file.is_file():
            continue
        rel = file.relative_to(workspace)
        try:
            with file.open("r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if regex.search(line):
                        matches.append(
                            {
                                "path": str(rel),
                                "line": lineno,
                                "text": line.rstrip("\n"),
                            }
                        )
                        if len(matches) >= MAX_GREP_RESULTS:
                            return _ok(
                                matches=matches,
                                truncated=True,
                                limit=MAX_GREP_RESULTS,
                            )
        except OSError:
            continue
    return _ok(matches=matches, truncated=False)


def run_shell(command: str) -> dict:
    """Run a shell command with the workspace as the working directory.

    The command runs through `/bin/sh -c`. Output is captured and returned.
    Long-running commands are killed after 60 seconds.

    Args:
        command: Shell command to execute.

    Returns:
        {"status": "success", "exit_code": int, "stdout": str, "stderr": str}
        or an error if the command times out or fails to launch.
    """
    workspace = get_workspace()
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _err(f"Command timed out after {SHELL_TIMEOUT_SECONDS}s.")
    except OSError as e:
        return _err(f"Failed to launch command: {e}")
    return _ok(
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


# Mimes Gemini happily reads as plain text. `text/*` is also
# text-like — checked separately so we don't enumerate every
# subtype. Anything not in this set falls through to the binary
# branch (saved as artifact, retrievable via `load_artifacts`).
_TEXT_LIKE_MIMES: frozenset[str] = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/javascript",
        "application/atom+xml",
        "application/rss+xml",
        "application/x-yaml",
        "application/yaml",
    }
)


def _is_text_like(content_type: str) -> bool:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    if not base:
        return False
    return base.startswith("text/") or base in _TEXT_LIKE_MIMES


def _fetch_artifact_filename(data: bytes, mime: str) -> str:
    """Deterministic name for `web_fetch`-saved bytes.

    The leading `_` is the gating signal `ChannelBase` uses to
    keep these artifacts off the channel outbound path — they're
    working data the model needs to read, not files we want to
    mail back to the user. The sha8 lets the agent dedupe across
    turns: same bytes → same filename → no double-save.
    """
    sha8 = hashlib.sha256(data).hexdigest()[:8]
    ext = mimetypes.guess_extension(mime or "") or ".bin"
    return f"_fetched_{sha8}{ext}"


async def web_fetch(url: str, tool_context: ToolContext) -> dict:
    """Fetch content from an HTTP(S) URL.

    Text-like responses (HTML, JSON, plain text, XML, etc.) are
    returned inline as `text`. Binary responses (images, PDFs,
    audio, video, archives) can't be stuffed into a JSON string
    without corrupting the bytes, so they're saved as a session
    artifact and you call `load_artifacts(artifact_names=[…])`
    next to surface the actual bytes for reasoning.

    Args:
        url: HTTP or HTTPS URL.

    Returns:
        Text path: {"status": "success", "url", "status_code",
        "content_type", "text", "truncated"}.
        Binary path: {"status": "success", "url", "status_code",
        "content_type", "saved_as_artifact": True, "filename",
        "mime", "bytes", "version", "hint"}. Call
        `load_artifacts` with the filename next to read it.
    """
    if not url.lower().startswith(("http://", "https://")):
        return _err("Only http:// and https:// URLs are supported.")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "adklaw/0.1 (+https://adk.dev)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(MAX_FETCH_BYTES + 1)
            content_type = response.headers.get("Content-Type", "")
            status_code = response.status
    except urllib.error.HTTPError as e:
        return _err(f"HTTP {e.code}: {e.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return _err(f"Fetch failed: {e}")

    if _is_text_like(content_type):
        return _web_fetch_text(url, status_code, content_type, raw)

    base_mime = (content_type or "").split(";", 1)[0].strip().lower()
    if not base_mime or base_mime == "application/octet-stream":
        # Mime missing or opaque. Try strict utf-8: if it works,
        # it's effectively text. Otherwise save as binary.
        try:
            text = raw[: MAX_FETCH_BYTES].decode("utf-8")
        except UnicodeDecodeError:
            return await _web_fetch_binary(
                url,
                status_code,
                content_type or "application/octet-stream",
                raw,
                tool_context,
            )
        truncated = len(raw) > MAX_FETCH_BYTES
        return _ok(
            url=url,
            status_code=status_code,
            content_type=content_type or "text/plain",
            text=text,
            truncated=truncated,
        )

    return await _web_fetch_binary(
        url, status_code, content_type, raw, tool_context
    )


def _web_fetch_text(
    url: str, status_code: int, content_type: str, raw: bytes
) -> dict:
    truncated = len(raw) > MAX_FETCH_BYTES
    if truncated:
        raw = raw[:MAX_FETCH_BYTES]
    text = raw.decode("utf-8", errors="replace")
    return _ok(
        url=url,
        status_code=status_code,
        content_type=content_type,
        text=text,
        truncated=truncated,
    )


async def _web_fetch_binary(
    url: str,
    status_code: int,
    content_type: str,
    raw: bytes,
    tool_context: ToolContext,
) -> dict:
    if len(raw) > MAX_FETCH_BYTES:
        return _err(
            f"Binary response too large ({len(raw)} bytes > "
            f"{MAX_FETCH_BYTES}). Cannot fetch."
        )
    mime = (content_type or "").split(";", 1)[0].strip() or (
        "application/octet-stream"
    )
    filename = _fetch_artifact_filename(raw, mime)
    try:
        version = await tool_context.save_artifact(
            filename=filename,
            artifact=types.Part(
                inline_data=types.Blob(data=raw, mime_type=mime)
            ),
        )
    except Exception as e:
        logger.exception("save_artifact failed for fetched url %s", url)
        return _err(f"Failed to save fetched bytes: {e}")
    return _ok(
        url=url,
        status_code=status_code,
        content_type=content_type,
        saved_as_artifact=True,
        filename=filename,
        mime=mime,
        bytes=len(raw),
        version=version,
        hint=(
            f'Binary content saved. Call load_artifacts('
            f'artifact_names=["{filename}"]) on your next turn '
            "to read it."
        ),
    )


@functools.cache
def _web_search_client() -> genai.Client:
    """Lazy module-level genai client for web_search.

    Cached so we construct the client (and dial Vertex auth) at most
    once per process. Tests clear this cache via the autouse fixture
    in `tests/conftest.py`.
    """
    return genai.Client()


def _parse_latlng(raw: str) -> types.LatLng | None:
    """Parse a `"lat,lng"` env var into a `types.LatLng`.

    Empty / blank string → no geographic bias. Malformed input → log
    a warning and fall back to no bias rather than failing the search.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        lat_str, lng_str = raw.split(",", 1)
        return types.LatLng(
            latitude=float(lat_str.strip()), longitude=float(lng_str.strip())
        )
    except (ValueError, AttributeError):
        logger.warning(
            "Invalid ADKLAW_WEB_SEARCH_LATLNG=%r; expected `lat,lng`. "
            "Falling back to no geographic bias.",
            raw,
        )
        return None


def web_search(query: str) -> dict:
    """Search the web via Gemini Flash-Lite with Google Search grounding.

    Returns a synthesized answer plus the list of cited sources.
    Geographic bias is set by `ADKLAW_WEB_SEARCH_LATLNG` (default
    Taipei `"25.0330,121.5654"`; empty string disables).

    Args:
        query: Free-form search query in any language.

    Returns:
        On success: `{"status": "success", "query": str, "answer": str,
        "sources": [{"title": str, "url": str}, ...],
        "search_queries": [str, ...]}`. On error: a `_err` dict.
    """
    if not query.strip():
        return _err("query must be non-empty")
    model = os.environ.get("ADKLAW_WEB_SEARCH_MODEL", WEB_SEARCH_MODEL_DEFAULT)
    latlng = _parse_latlng(
        os.environ.get("ADKLAW_WEB_SEARCH_LATLNG", WEB_SEARCH_LATLNG_DEFAULT)
    )
    config_kwargs: dict = {
        "tools": [types.Tool(google_search=types.GoogleSearch())],
    }
    if latlng is not None:
        config_kwargs["tool_config"] = types.ToolConfig(
            retrieval_config=types.RetrievalConfig(lat_lng=latlng),
        )
    try:
        response = _web_search_client().models.generate_content(
            model=model,
            contents=query,
            config=types.GenerateContentConfig(**config_kwargs),
        )
    except Exception as e:
        return _err(f"Search failed: {e}")

    answer = (response.text or "").strip()
    if not answer:
        return _err("Search returned no text (possibly blocked).")

    sources: list[dict] = []
    queries: list[str] = []
    candidates = response.candidates or []
    cand = candidates[0] if candidates else None
    gm = getattr(cand, "grounding_metadata", None) if cand else None
    if gm is not None:
        seen: set[str] = set()
        for chunk in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(chunk, "web", None)
            if web is None:
                continue
            url = getattr(web, "uri", None) or ""
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(
                {"title": getattr(web, "title", "") or "", "url": url}
            )
        queries = list(getattr(gm, "web_search_queries", None) or [])

    return _ok(
        query=query,
        answer=answer,
        sources=sources,
        search_queries=queries,
    )


@functools.cache
def _send_file_max_bytes() -> int:
    """Largest file `send_workspace_file` will attach. We fail the
    tool call rather than letting the channel layer silently drop a
    file mid-reply. Configurable via `ADKLAW_SEND_FILE_MAX_BYTES`."""
    raw = os.environ.get("ADKLAW_SEND_FILE_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_SEND_FILE_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "Invalid ADKLAW_SEND_FILE_MAX_BYTES=%r; defaulting to %d.",
            raw,
            DEFAULT_SEND_FILE_MAX_BYTES,
        )
        return DEFAULT_SEND_FILE_MAX_BYTES


async def send_workspace_file(
    path: str, tool_context: ToolContext
) -> dict:
    """Attach a file from the workspace to your reply.

    Use this when the user asks you to send, share, attach, or
    give them a specific file. The file is delivered as a real
    attachment on the channel (e.g. as a Discord upload), not
    pasted into the reply text. Works for any file type — images,
    PDFs, audio, archives, source code, binaries.

    Args:
        path: File path, relative to the workspace or an absolute path
            inside it.

    Returns:
        On success: {"status": "success", "filename": str, "mime": str,
        "bytes": int, "version": int}.
        On error: {"status": "error", "error": str}.
    """
    try:
        target = resolve_in_workspace(path)
    except ValueError as e:
        return _err(str(e))
    if not target.exists():
        return _err(f"File does not exist: {path}")
    if not target.is_file():
        return _err(f"Not a file: {path}")
    cap = _send_file_max_bytes()
    try:
        size = target.stat().st_size
    except OSError as e:
        return _err(f"Stat failed: {e}")
    if size > cap:
        return _err(
            f"File too large ({size} bytes > {cap}). "
            "Adjust ADKLAW_SEND_FILE_MAX_BYTES or split the file."
        )
    try:
        data = target.read_bytes()
    except OSError as e:
        return _err(f"Read failed: {e}")
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    try:
        version = await tool_context.save_artifact(
            filename=target.name,
            artifact=types.Part(
                inline_data=types.Blob(data=data, mime_type=mime)
            ),
        )
    except Exception as e:
        logger.exception("save_artifact failed for %s", target)
        return _err(f"Failed to save artifact: {e}")
    return _ok(
        filename=target.name,
        mime=mime,
        bytes=len(data),
        version=version,
    )


async def list_knowledge() -> dict:
    """List the slugs and one-line summaries of every entry in the
    knowledge store. Use this to discover what durable facts you
    have already recorded; then read the relevant ones with
    `read_knowledge(slug)`.

    Returns a list sorted by `created_at` ascending — oldest first,
    new entries always appended."""
    from .knowledge import get_knowledge_service

    service = get_knowledge_service()
    entries = await service.list_knowledge()
    return _ok(
        entries=[
            {
                "slug": e.slug,
                "summary": e.summary,
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ]
    )


async def read_knowledge(slug: str) -> dict:
    """Read a single knowledge entry by its slug.

    Returns the full markdown content along with the summary and
    timestamps. Returns `status: "error"` if the slug doesn't
    exist."""
    from .knowledge import get_knowledge_service
    from .knowledge.local import InvalidSlugError

    service = get_knowledge_service()
    try:
        entry = await service.read_knowledge(slug)
    except InvalidSlugError as e:
        return _err(str(e))
    if entry is None:
        return _err(f"no knowledge entry with slug {slug!r}")
    return _ok(
        slug=entry.slug,
        summary=entry.summary,
        content=entry.content,
        created_at=entry.created_at.isoformat(),
        updated_at=entry.updated_at.isoformat(),
    )


async def write_knowledge(slug: str, summary: str, content: str) -> dict:
    """Create or update a knowledge entry.

    `slug` must match `[a-z0-9][a-z0-9_-]*` (kebab-case). It's the
    stable identifier — pick something descriptive and short.

    `summary` is a one-line description (≤120 chars recommended)
    that goes into the prompt index every turn. Keep it stable
    across content edits — rewriting the summary on minor changes
    invalidates the prompt cache.

    `content` is freeform markdown; pick the structure that fits
    the fact you're recording. No required headings.
    """
    from .knowledge import get_knowledge_service
    from .knowledge.local import InvalidSlugError

    service = get_knowledge_service()
    try:
        entry = await service.write_knowledge(slug, summary, content)
    except InvalidSlugError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"failed to write knowledge entry {slug!r}: {e}")
    return _ok(
        slug=entry.slug,
        summary=entry.summary,
        created_at=entry.created_at.isoformat(),
        updated_at=entry.updated_at.isoformat(),
    )


async def delete_knowledge(slug: str) -> dict:
    """Delete a knowledge entry. Returns `status: "error"` if the
    slug doesn't exist (so the agent can detect typos)."""
    from .knowledge import get_knowledge_service
    from .knowledge.local import InvalidSlugError

    service = get_knowledge_service()
    try:
        existed = await service.delete_knowledge(slug)
    except InvalidSlugError as e:
        return _err(str(e))
    if not existed:
        return _err(f"no knowledge entry with slug {slug!r}")
    return _ok(slug=slug)


ALL_TOOLS = [
    read_file,
    write_file,
    edit_file,
    undo_last_edit,
    list_dir,
    glob_files,
    grep,
    run_shell,
    web_fetch,
    web_search,
    send_workspace_file,
    list_knowledge,
    read_knowledge,
    write_knowledge,
    delete_knowledge,
]
