"""Local filesystem-backed knowledge service.

Stores entries as markdown files with YAML frontmatter under
`<workspace>/.knowledge/<slug>.md`. The hidden directory signals
"managed by the agent; touch carefully" — humans can edit via
filesystem (mtime check picks up out-of-band changes) but the
expected path is round-tripping through `read_knowledge` /
`write_knowledge` so the in-memory index stays in sync.

File layout:

    ---
    summary: <one-line summary, ≤120 chars>
    created_at: <ISO8601>
    updated_at: <ISO8601>
    ---
    <freeform markdown content>

`summary` is the source of truth for the prompt index.
`created_at` drives index ordering (append-only).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .base import BaseKnowledgeService, KnowledgeEntry, KnowledgeIndexEntry

_FRONTMATTER_RE = re.compile(
    r"\A---\n(?P<frontmatter>.*?)\n---\n(?P<body>.*)", re.DOTALL
)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class InvalidSlugError(ValueError):
    """Raised when a slug doesn't match the allowed character set."""


def _validate_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise InvalidSlugError(
            f"invalid slug {slug!r}: must match [a-z0-9][a-z0-9_-]*"
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(entry: KnowledgeEntry) -> str:
    frontmatter = yaml.safe_dump(
        {
            "summary": entry.summary,
            "created_at": entry.created_at.isoformat(),
            "updated_at": entry.updated_at.isoformat(),
        },
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    return f"---\n{frontmatter}\n---\n{entry.content}"


def _parse(slug: str, raw: str) -> KnowledgeEntry:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError(
            f"entry {slug!r} is missing the YAML frontmatter block"
        )
    meta = yaml.safe_load(match.group("frontmatter")) or {}
    summary = meta.get("summary", "")
    created_at = _coerce_datetime(meta.get("created_at"))
    updated_at = _coerce_datetime(meta.get("updated_at")) or created_at
    if created_at is None:
        raise ValueError(f"entry {slug!r} is missing created_at")
    return KnowledgeEntry(
        slug=slug,
        summary=summary,
        content=match.group("body"),
        created_at=created_at,
        updated_at=updated_at,
    )


def _coerce_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError(f"unsupported datetime value: {value!r}")


class LocalKnowledgeBackend(BaseKnowledgeService):
    """Knowledge stored as markdown files under
    `<root>/.knowledge/`.

    The directory is created on first write. The in-memory index
    is rebuilt when any file's mtime is newer than the cache
    timestamp — this picks up out-of-band edits across session
    starts without needing a watch.
    """

    def __init__(self, root: Path) -> None:
        self._dir = Path(root) / ".knowledge"
        self._index_cache: list[KnowledgeIndexEntry] | None = None
        self._index_built_at: float = 0.0

    @property
    def directory(self) -> Path:
        return self._dir

    def _path_for(self, slug: str) -> Path:
        _validate_slug(slug)
        return self._dir / f"{slug}.md"

    def _invalidate_index(self) -> None:
        self._index_cache = None

    def _scan(self) -> list[KnowledgeIndexEntry]:
        entries: list[KnowledgeIndexEntry] = []
        if not self._dir.is_dir():
            return entries
        for path in self._dir.glob("*.md"):
            slug = path.stem
            try:
                _validate_slug(slug)
            except InvalidSlugError:
                continue
            try:
                entry = _parse(slug, path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                # Skip malformed files rather than blow up the index;
                # the agent can still use the rest of the store.
                continue
            entries.append(
                KnowledgeIndexEntry(
                    slug=entry.slug,
                    summary=entry.summary,
                    created_at=entry.created_at,
                )
            )
        entries.sort(key=lambda e: (e.created_at, e.slug))
        return entries

    def _dir_mtime(self) -> float:
        if not self._dir.exists():
            return 0.0
        latest = self._dir.stat().st_mtime
        for path in self._dir.glob("*.md"):
            latest = max(latest, path.stat().st_mtime)
        return latest

    async def list_knowledge(self) -> list[KnowledgeIndexEntry]:
        latest_mtime = self._dir_mtime()
        if (
            self._index_cache is not None
            and latest_mtime <= self._index_built_at
        ):
            return list(self._index_cache)
        self._index_cache = self._scan()
        self._index_built_at = latest_mtime
        return list(self._index_cache)

    async def read_knowledge(self, slug: str) -> KnowledgeEntry | None:
        path = self._path_for(slug)
        if not path.is_file():
            return None
        return _parse(slug, path.read_text(encoding="utf-8"))

    async def write_knowledge(
        self, slug: str, summary: str, content: str
    ) -> KnowledgeEntry:
        path = self._path_for(slug)
        existing = await self.read_knowledge(slug)
        now = _now()
        entry = KnowledgeEntry(
            slug=slug,
            summary=summary,
            content=content,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text(_serialize(entry), encoding="utf-8")
        self._invalidate_index()
        return entry

    async def delete_knowledge(self, slug: str) -> bool:
        path = self._path_for(slug)
        if not path.is_file():
            return False
        path.unlink()
        self._invalidate_index()
        return True
