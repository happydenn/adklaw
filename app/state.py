# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Agent state directory resolution.

The state directory holds **internal** agent machinery — session DB,
channel state, future caches — kept deliberately separate from the
`workspace/` directory which is the human-agent collaboration surface.

By default it lives at `<project>/.adklaw/` (gitignored). Override with
`ADKLAW_STATE_DIR` to relocate (e.g. `~/.adklaw/` or a tmpfs).
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = PROJECT_ROOT / ".adklaw"


def get_state_dir() -> Path:
    """Return the resolved agent state directory, creating it if missing."""
    raw = os.environ.get("ADKLAW_STATE_DIR")
    path = Path(raw).expanduser().resolve() if raw else DEFAULT_STATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path
