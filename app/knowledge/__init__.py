"""Knowledge service (Tier 3 semantic memory).

Durable structured facts the agent learns and reuses across
sessions. See `docs/knowledge.md` for the full design.
"""

from .base import BaseKnowledgeService, KnowledgeEntry, KnowledgeIndexEntry
from .local import LocalKnowledgeBackend
from .memory import InMemoryKnowledgeBackend
from .service import (
    DEFAULT_FIRESTORE_COLLECTION,
    get_knowledge_service,
    render_index,
)

__all__ = [
    "BaseKnowledgeService",
    "DEFAULT_FIRESTORE_COLLECTION",
    "InMemoryKnowledgeBackend",
    "KnowledgeEntry",
    "KnowledgeIndexEntry",
    "LocalKnowledgeBackend",
    "get_knowledge_service",
    "render_index",
]
# `FirestoreKnowledgeBackend` is imported lazily by `service.py`
# when the `firestore` backend is selected, so a default import
# of `app.knowledge` doesn't pull in `google.cloud.firestore`.
# Tests and explicit users import it directly:
#     from app.knowledge.firestore import FirestoreKnowledgeBackend
