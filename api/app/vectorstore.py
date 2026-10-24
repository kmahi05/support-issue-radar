"""ChromaDB client, the `tickets` and `issues` collections (§3.4), and the tenant filter.

Chroma has no RLS: every query/update/delete `where` is built by tenant_where(), nowhere else.
"""

from typing import Any
from uuid import UUID

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection

COLLECTIONS = ("tickets", "issues")
SCHEMA_VERSION = 1


def tenant_where(org_id: UUID | str, **conditions: Any) -> dict:
    """Build a Chroma `where` that always ANDs the org filter with the given conditions."""
    if not org_id:
        raise ValueError("tenant_where requires an org_id")
    clauses: list[dict] = [{"org_id": str(org_id)}]
    clauses += [{key: value} for key, value in conditions.items()]
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def open_client(path: str) -> ClientAPI:
    return chromadb.PersistentClient(path=path)


def open_collections(client: ClientAPI, embedding_model: str) -> dict[str, Collection]:
    """Get or create both collections; refuse to start if they were built with another model."""
    existing = {c.name if hasattr(c, "name") else c for c in client.list_collections()}
    cols: dict[str, Collection] = {}
    for name in COLLECTIONS:
        if name in existing:
            col = client.get_collection(name)
            stored = (col.metadata or {}).get("embedding_model")
            if stored != embedding_model:
                raise RuntimeError(
                    f"Chroma collection '{name}' was built with embedding model {stored!r}, "
                    f"but GEMINI_EMBEDDING_MODEL is {embedding_model!r}. "
                    "Set it back, or point CHROMA_PATH at an empty directory and rebuild with "
                    "POST /api/v1/admin/reindex (Postgres is the source of truth)."
                )
        else:
            col = client.create_collection(
                name,
                configuration={"hnsw": {"space": "cosine"}},
                metadata={"embedding_model": embedding_model, "schema_version": SCHEMA_VERSION},
            )
        cols[name] = col
    return cols
