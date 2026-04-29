"""Knowledge service singleton + prompt-index rendering.

The backend is selected by `ADKLAW_KNOWLEDGE_BACKEND` (default
`local`). The singleton is rebuilt when the workspace,
collection, or project changes, so tests that point at a
fresh `mktemp -d` or override Firestore wiring don't get a
stale backend.

Env var contract:

  ADKLAW_KNOWLEDGE_BACKEND
      `local` (default) | `firestore`
  ADKLAW_KNOWLEDGE_FIRESTORE_COLLECTION
      Firestore collection name. Default: `adklaw_knowledge`.
  ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT
      GCP project for the Firestore database. Falls back to
      `GOOGLE_CLOUD_PROJECT` if unset. If neither is set,
      Firestore-backend instantiation raises `ValueError`.

`render_index` is what the instruction provider calls each turn
to build layer 6 of the system prompt (see
`docs/instruction-layering.md`).
"""

from __future__ import annotations

import os
from functools import lru_cache

from ..workspace import get_workspace
from .base import BaseKnowledgeService, KnowledgeIndexEntry
from .local import LocalKnowledgeBackend

DEFAULT_FIRESTORE_COLLECTION = "adklaw_knowledge"


@lru_cache(maxsize=1)
def _build_service(
    backend_name: str,
    workspace_str: str,
    firestore_project: str | None,
    firestore_collection: str,
) -> BaseKnowledgeService:
    """Cache-keyed by (backend, workspace, project, collection) so
    overriding any of those produces a fresh service."""
    if backend_name == "local":
        from pathlib import Path

        return LocalKnowledgeBackend(Path(workspace_str))
    if backend_name == "firestore":
        if not firestore_project:
            raise ValueError(
                "Firestore knowledge backend requires a GCP project. "
                "Set ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT or "
                "GOOGLE_CLOUD_PROJECT."
            )
        from .firestore import FirestoreKnowledgeBackend

        return FirestoreKnowledgeBackend(
            project=firestore_project,
            collection=firestore_collection,
        )
    raise ValueError(f"unknown knowledge backend: {backend_name!r}")


def get_knowledge_service() -> BaseKnowledgeService:
    backend = os.environ.get("ADKLAW_KNOWLEDGE_BACKEND", "local").lower()
    workspace = str(get_workspace())
    project = os.environ.get(
        "ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT"
    ) or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""
    collection = os.environ.get(
        "ADKLAW_KNOWLEDGE_FIRESTORE_COLLECTION",
        DEFAULT_FIRESTORE_COLLECTION,
    )
    return _build_service(backend, workspace, project or None, collection)


def render_index(entries: list[KnowledgeIndexEntry]) -> str:
    """Render the knowledge index for the system prompt.

    Empty when the store has no entries — the agent doesn't need
    to know an empty store exists, and emitting an empty section
    just costs tokens.

    Format is intentionally compact and stable: one bullet per
    entry, slug in backticks, summary as plain prose. No
    timestamps, no volatile fields — see
    `docs/instruction-layering.md` for cache-stability rules.
    """
    if not entries:
        return ""
    lines = [
        "## Knowledge index",
        "",
        "Durable facts you have recorded. Read with `read_knowledge(slug)`;",
        "create or update with `write_knowledge(slug, summary, content)`.",
        "",
    ]
    for entry in entries:
        summary = entry.summary or "(no summary)"
        lines.append(f"- `{entry.slug}` — {summary}")
    return "\n".join(lines)
