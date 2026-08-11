"""ChromaDB RAG client for ARTA semantic search."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ChromaRAG:
    """Wrapper around ChromaDB for embedding and querying ARTA artefacts.

    Collections:
      - requirements: parsed requirement text + metadata
      - test_scenarios: Gherkin scenarios + metadata
      - defects: defect titles + root causes
    """

    COLLECTIONS = ("requirements", "test_scenarios", "defects")

    def __init__(self, chroma_url: str = "http://chromadb:8001"):
        self._url = chroma_url
        self._client = None
        self._collections: dict[str, Any] = {}

    async def connect(self) -> bool:
        """Initialise ChromaDB client and ensure collections exist."""
        try:
            import chromadb

            self._client = chromadb.HttpClient(host=self._url.replace("http://", "").split(":")[0],
                                                port=int(self._url.rsplit(":", 1)[-1]))
            # Try Ollama embedding function, fall back to default
            ef = self._get_embedding_function()
            for name in self.COLLECTIONS:
                self._collections[name] = self._client.get_or_create_collection(
                    name=name,
                    embedding_function=ef,
                    metadata={"hnsw:space": "cosine"},
                )
            logger.info("ChromaDB connected at %s with %d collections", self._url, len(self._collections))
            return True
        except Exception as exc:
            logger.warning("ChromaDB unavailable (%s) — RAG features disabled", exc)
            self._client = None
            return False

    @property
    def available(self) -> bool:
        return self._client is not None

    # ── Embedding functions ──────────────────────────────────────────────────

    @staticmethod
    def _get_embedding_function():
        """Try Ollama nomic-embed-text, fall back to ChromaDB default."""
        try:
            from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
            return OllamaEmbeddingFunction(
                url="http://host.docker.internal:11434/api/embeddings",
                model_name="nomic-embed-text",
            )
        except Exception:
            return None  # ChromaDB will use its default all-MiniLM-L6-v2

    # ── Embed operations ─────────────────────────────────────────────────────

    def embed_requirement(self, req_id: str, text: str, metadata: dict | None = None) -> None:
        """Add or update a requirement in the requirements collection.

        Skips re-embedding if content_hash in metadata matches the stored hash
        (avoids unnecessary embedding computation for unchanged requirements).
        """
        if not self.available:
            return
        meta = metadata or {}
        new_hash = meta.get("content_hash")
        if new_hash:
            coll = self._collections["requirements"]
            try:
                existing = coll.get(ids=[req_id], include=["metadatas"])
                if existing and existing["ids"] and existing["metadatas"]:
                    old_hash = existing["metadatas"][0].get("content_hash")
                    if old_hash == new_hash:
                        return  # Content unchanged — skip re-embedding
            except Exception:
                pass  # Collection may not have the record yet
        coll = self._collections["requirements"]
        coll.upsert(
            ids=[req_id],
            documents=[text],
            metadatas=[meta],
        )

    def embed_test_scenario(self, scenario_id: str, text: str, metadata: dict | None = None) -> None:
        """Add or update a test scenario in the test_scenarios collection."""
        if not self.available:
            return
        coll = self._collections["test_scenarios"]
        coll.upsert(
            ids=[scenario_id],
            documents=[text],
            metadatas=[metadata or {}],
        )

    def embed_defect(self, defect_id: str, text: str, metadata: dict | None = None) -> None:
        """Add or update a defect in the defects collection."""
        if not self.available:
            return
        coll = self._collections["defects"]
        coll.upsert(
            ids=[defect_id],
            documents=[text],
            metadatas=[metadata or {}],
        )

    # ── Query operations ─────────────────────────────────────────────────────

    def query_similar_requirements(self, text: str, n: int = 5) -> list[dict]:
        """Find requirements semantically similar to the given text."""
        return self._query("requirements", text, n)

    def query_similar_scenarios(self, text: str, n: int = 5) -> list[dict]:
        """Find test scenarios semantically similar to the given text."""
        return self._query("test_scenarios", text, n)

    def query_similar_defects(self, text: str, n: int = 5) -> list[dict]:
        """Find defects semantically similar to the given text."""
        return self._query("defects", text, n)

    def _query(self, collection_name: str, text: str, n: int) -> list[dict]:
        if not self.available:
            return []
        coll = self._collections.get(collection_name)
        if not coll:
            return []
        try:
            results = coll.query(query_texts=[text], n_results=n)
            items = []
            for i, doc_id in enumerate(results["ids"][0]):
                items.append({
                    "id": doc_id,
                    "document": results["documents"][0][i] if results.get("documents") else "",
                    "metadata": results["metadatas"][0][i] if results.get("metadatas") else {},
                    "distance": results["distances"][0][i] if results.get("distances") else None,
                })
            return items
        except Exception as exc:
            logger.warning("ChromaDB query failed for %s: %s", collection_name, exc)
            return []
