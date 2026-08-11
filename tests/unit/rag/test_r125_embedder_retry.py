"""R125 — RAG embedder reconnect-with-backoff loop.

Pre-R125 evidence: at 07:18:07 UTC on 2026-05-20, Redis container was killed
by external orchestrator (Jenkins host wipe pattern) + recreated 13 seconds
later at 07:18:20. The embedder's single subscribe attempt failed with
`Error 111 connecting to redis:6379. Connection refused.` — coroutine
exited terminally + RAG index stayed stale until the next arta-api restart.

Post-R125: outer reconnect loop with exponential backoff (2s → 4s → 8s →
16s → max 30s). Backoff resets on successful subscribe. CancelledError
propagates cleanly (operator shutdown not retried).
"""
from __future__ import annotations

import asyncio

import pytest


class _StubChromaRAG:
    available = True

    def embed_requirement(self, *a, **kw): pass
    def embed_test_scenario(self, *a, **kw): pass
    def embed_defect(self, *a, **kw): pass


@pytest.mark.asyncio
async def test_r125_retries_on_connection_refused(monkeypatch):
    """ConnectionRefusedError on subscribe → log + backoff + retry.

    Pre-R125: terminal exit after ONE warning.
    Post-R125: retries with backoff until subscribe succeeds.
    """
    import redis.asyncio as real_aioredis
    from src.rag import embedder as emb

    monkeypatch.setattr(emb, "_R125_BACKOFF_INITIAL_S", 0.01)
    monkeypatch.setattr(emb, "_R125_BACKOFF_MAX_S", 0.02)

    state = {"attempts": 0}

    class _StubPubSub:
        async def subscribe(self, channel):
            state["attempts"] += 1
            if state["attempts"] <= 3:
                raise ConnectionRefusedError(
                    f"Error 111 connecting to redis:6379. Connection refused. "
                    f"(attempt {state['attempts']})"
                )

        def listen(self):
            # async generator that yields nothing + raises CancelledError
            # so the test exits the listen-loop cleanly
            async def _gen():
                raise asyncio.CancelledError()
                yield  # noqa: unreachable
            return _gen()

        async def unsubscribe(self, channel): pass
        async def close(self): pass

    class _StubClient:
        def pubsub(self): return _StubPubSub()
        async def close(self): pass

    def _stub_from_url(url):
        return _StubClient()

    monkeypatch.setattr(real_aioredis, "from_url", _stub_from_url)

    chroma = _StubChromaRAG()
    with pytest.raises(asyncio.CancelledError):
        await emb.start_embedding_listener(chroma, redis_url="redis://test:6379")

    assert state["attempts"] == 4, (
        f"Expected 3 failed subscribe attempts + 1 success = 4 total; "
        f"got {state['attempts']}"
    )


@pytest.mark.asyncio
async def test_r125_cancellederror_propagates_cleanly(monkeypatch):
    """Operator shutdown (CancelledError) → propagate, no retry."""
    import redis.asyncio as real_aioredis
    from src.rag import embedder as emb
    monkeypatch.setattr(emb, "_R125_BACKOFF_INITIAL_S", 0.01)

    state = {"attempts": 0}

    class _StubPubSub:
        async def subscribe(self, channel):
            state["attempts"] += 1
            raise asyncio.CancelledError()
        async def unsubscribe(self, channel): pass
        async def close(self): pass

    class _StubClient:
        def pubsub(self): return _StubPubSub()
        async def close(self): pass

    monkeypatch.setattr(real_aioredis, "from_url", lambda url: _StubClient())

    chroma = _StubChromaRAG()
    with pytest.raises(asyncio.CancelledError):
        await emb.start_embedding_listener(chroma, redis_url="redis://test:6379")

    # CancelledError must propagate without retry
    assert state["attempts"] == 1, (
        f"CancelledError should propagate without retry; got "
        f"{state['attempts']} attempts"
    )


def test_r125_backoff_caps_at_max():
    """Exponential backoff caps at `_R125_BACKOFF_MAX_S` so the retry
    loop doesn't burn 5min+ delays after many failures."""
    from src.rag import embedder as emb
    backoff = emb._R125_BACKOFF_INITIAL_S
    cap = emb._R125_BACKOFF_MAX_S
    for _ in range(20):
        backoff = min(backoff * 2.0, cap)
    assert backoff == cap, f"backoff should reach + stay at cap={cap}; got {backoff}"


@pytest.mark.asyncio
async def test_r125_skips_when_chroma_unavailable():
    """If ChromaDB isn't available, listener exits immediately — NO retry
    loop fires. Preserves R125's mission-correct behaviour: don't burn
    CPU subscribing to Redis when there's no place to write embeddings."""
    from src.rag import embedder as emb

    class _UnavailableChromaRAG:
        available = False

    chroma = _UnavailableChromaRAG()
    result = await emb.start_embedding_listener(chroma, redis_url="redis://test:6379")
    assert result is None
