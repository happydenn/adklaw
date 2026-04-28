# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Common tools for the adklaw assistant.

All filesystem tools are rooted at the configured workspace and reject paths
that would escape it. `run_shell` executes with the workspace as cwd. All
tools return plain dicts so the LLM gets structured feedback on success or
failure without raising exceptions.
"""

from __future__ import annotations

import re
import subprocess
import urllib.error
import urllib.request

from .workspace import get_workspace, resolve_in_workspace

MAX_FILE_BYTES = 1_000_000  # 1 MB read cap
MAX_GREP_RESULTS = 200
MAX_FETCH_BYTES = 2_000_000  # 2 MB fetch cap
SHELL_TIMEOUT_SECONDS = 60


def _ok(**fields) -> dict:
    return {"status": "success", **fields}


def _err(message: str) -> dict:
    return {"status": "error", "error": message}


def read_file(path: str) -> dict:
    """Read a text file from the workspace.

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


def edit_file(path: str, old_string: str, new_string: str) -> dict:
    """Replace exactly one occurrence of `old_string` with `new_string`.

    Args:
        path: Existing file inside the workspace.
        old_string: Exact string to find. Must occur exactly once.
        new_string: Replacement string.

    Returns:
        Success dict with `path` on replacement, or an error if `old_string`
        is missing or appears multiple times.
    """
    try:
        target = resolve_in_workspace(path)
    except ValueError as e:
        return _err(str(e))
    if not target.is_file():
        return _err(f"Not a file: {path}")
    try:
        original = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return _err(f"Read failed: {e}")
    occurrences = original.count(old_string)
    if occurrences == 0:
        return _err("old_string not found in file.")
    if occurrences > 1:
        return _err(
            f"old_string is not unique ({occurrences} matches). "
            "Provide more surrounding context."
        )
    updated = original.replace(old_string, new_string, 1)
    try:
        target.write_text(updated, encoding="utf-8")
    except OSError as e:
        return _err(f"Write failed: {e}")
    return _ok(path=str(target))


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


def web_fetch(url: str) -> dict:
    """Fetch content from an HTTP(S) URL.

    Args:
        url: HTTP or HTTPS URL.

    Returns:
        {"status": "success", "url", "status_code", "content_type", "text"} or
        an error. Response is truncated at 2 MB.
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

    truncated = len(raw) > MAX_FETCH_BYTES
    if truncated:
        raw = raw[:MAX_FETCH_BYTES]
    try:
        text = raw.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return _err("Response is not decodable as text.")
    return _ok(
        url=url,
        status_code=status_code,
        content_type=content_type,
        text=text,
        truncated=truncated,
    )


ALL_TOOLS = [
    read_file,
    write_file,
    edit_file,
    list_dir,
    glob_files,
    grep,
    run_shell,
    web_fetch,
]
