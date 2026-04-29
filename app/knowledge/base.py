"""Knowledge service interface and shared types.

The interface is deliberately small: list / read / write / delete.
Search is deferred — the system-prompt index is the discovery
mechanism for v1, and when the corpus outgrows it, search graduates
per-backend natively (see `docs/knowledge.md`).

Slugs are stable identifiers (filename for Local, document id for
Firestore). The agent supplies the summary at write time so the
index stays accurate without a separate summarization pass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class KnowledgeIndexEntry:
    """Lightweight projection used to build the prompt index.

    Backends are expected to return these without loading full
    content (Firestore: project the `summary` field; Local: parse
    YAML frontmatter only). Sort order at the storage layer is
    `created_at` ascending — append-only, so existing entries
    don't shift in the rendered index.
    """

    slug: str
    summary: str
    created_at: datetime


@dataclass(frozen=True)
class KnowledgeEntry:
    """Full knowledge entry as returned by `read_knowledge`."""

    slug: str
    summary: str
    content: str
    created_at: datetime
    updated_at: datetime


class BaseKnowledgeService(ABC):
    """Abstract knowledge backend.

    Backends:
      - `LocalKnowledgeBackend` — markdown files under
        `<workspace>/.knowledge/`. Used in local dev / Cloud Run
        with a persistent volume.
      - `FirestoreKnowledgeBackend` — Firestore collection.
        Used for stateless deployments. Lands in PR 4.
      - `InMemoryKnowledgeBackend` — for tests; ephemeral.
    """

    @abstractmethod
    async def list_knowledge(self) -> list[KnowledgeIndexEntry]:
        """Return the index of all entries, sorted by `created_at`
        ascending (oldest first). Append-only ordering — new entries
        land at the end so the rendered prompt index doesn't shift."""

    @abstractmethod
    async def read_knowledge(self, slug: str) -> KnowledgeEntry | None:
        """Return the full entry for `slug`, or None if not found."""

    @abstractmethod
    async def write_knowledge(
        self, slug: str, summary: str, content: str
    ) -> KnowledgeEntry:
        """Create or update an entry. The agent supplies the summary
        explicitly so the prompt index stays accurate without a
        separate summarization pass.

        For new entries, `created_at` is set to now. For updates,
        `created_at` is preserved from the existing entry; only
        `updated_at` advances. This keeps the index ordering
        append-only for cache stability.
        """

    @abstractmethod
    async def delete_knowledge(self, slug: str) -> bool:
        """Delete the entry. Returns True if it existed and was
        deleted, False if no such entry."""
