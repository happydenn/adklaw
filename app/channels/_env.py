# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Auto-load `.env` for any channel process.

Called once at `app.channels` package import time, before any concrete
channel module imports `app.agent`. Mirrors the behavior of ADK's
`google.adk.cli.utils.envs.load_dotenv_for_agent` so channels that run
outside of `agents-cli` (e.g. `python -m app.channels.discord`) get the
same `.env` discovery as `agents-cli run` and `agents-cli playground`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

_DISABLE_VAR = "ADK_DISABLE_LOAD_DOTENV"

logger = logging.getLogger(__name__)


def load_channel_env() -> None:
    """Find and load the project's `.env` file.

    Walks from this file's directory upward looking for `.env`. Existing
    environment variables are preserved (`override=False`), matching
    ADK's behavior. Honors `ADK_DISABLE_LOAD_DOTENV=1` as an opt-out.
    """
    if os.environ.get(_DISABLE_VAR):
        return
    found = find_dotenv(
        filename=".env",
        usecwd=False,
        raise_error_if_not_found=False,
    )
    if found:
        load_dotenv(found, override=False)
        logger.debug("Loaded .env from %s", Path(found).resolve())
