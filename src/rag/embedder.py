"""Background embedding service — listens to Redis pub/sub for new artefacts and embeds them.

R125 — resilient reconnect loop. Pre-R125: a single Redis blip (Jenkins/host
container wipe → Redis recreated in 13s) killed the embedder permanently for
the lifetime of the arta-api process. The handler caught the ConnectionError,
logged ONE warning, then exited the coroutine — leaving ARTA's RAG index
stale until the next arta-api restart.

Post-R125: outer retry loop with exponential backoff (2s → 4s → 8s → 16s →
max 30s). Backoff resets on successful subscribe so a long-lived listener
that ALSO bounces later doesn't burn a long delay starting fresh.
`asyncio.CancelledError` propagates cleanly (operator shutdown is not
retried). All other exceptions get logged + retried.
"""
from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

_R125_BACKOFF_INITIAL_S = 2.0
_R125_BACKOFF_MAX_S = 30.0


async def start_embedding_listener(chroma_rag, redis_url: str = "redis://localhost:6379") -> None:
    """Subscribe to ARTA artefact events and embed them into ChromaDB.

    Expected message format on channel `arta:artefacts`:
      {"type": "requirement"|"scenario"|"defect", "id": "...", "text": "...", "metadata": {...}}

    R125: reconnect-with-backoff loop survives Redis container restarts +
    transient network blips. Existing behaviour preserved for the happy
    path — subscribe + listen + cleanup. New behaviour on failure: log +
    backoff + reconnect instead of terminal exit.
    """
    try:
        import redis.asyncio as aioredis
    except ImportError:
        logger.warning("redis.asyncio not available — background embedder disabled")
        return

    if not chroma_rag or not chroma_rag.available:
        logger.info("ChromaDB not available — skipping embedding listener")
        return

    backoff_s = _R125_BACKOFF_INITIAL_S
    while True:
        client = None
        pubsub = None
        try:
            client = aioredis.from_url(redis_url)
            pubsub = client.pubsub()
            await pubsub.subscribe("arta:artefacts")
            logger.info("R125: Embedding listener subscribed to arta:artefacts")
            # Successful subscribe → reset backoff so a later disconnect
            # starts retrying at the initial cadence, not the saved value.
            backoff_s = _R125_BACKOFF_INITIAL_S

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    artefact_type = data.get("type")
                    artefact_id = data.get("id")
                    text = data.get("text", "")
                    metadata = data.get("metadata", {})

                    if artefact_type == "requirement":
                        chroma_rag.embed_requirement(artefact_id, text, metadata)
                    elif artefact_type == "scenario":
                        chroma_rag.embed_test_scenario(artefact_id, text, metadata)
                    elif artefact_type == "defect":
                        chroma_rag.embed_defect(artefact_id, text, metadata)

                    logger.debug("Embedded %s %s", artefact_type, artefact_id)
                except Exception as exc:
                    logger.warning("Failed to process embedding message: %s", exc)
        except asyncio.CancelledError:
            logger.info("R125: Embedding listener cancelled — shutting down cleanly")
            raise   # propagate; no retry on operator shutdown
        except Exception as exc:
            logger.warning(
                "R125: Embedding listener disconnected (%s: %s) — reconnect in %.1fs",
                type(exc).__name__, exc, backoff_s,
            )
        finally:
            # Clean up Redis connection to prevent "aclose() already running"
            # warnings on the next reconnect attempt.
            if pubsub:
                try:
                    await pubsub.unsubscribe("arta:artefacts")
                    await pubsub.close()
                except Exception:
                    pass
            if client:
                try:
                    await client.close()
                except Exception:
                    pass

        # Backoff before next reconnect attempt. asyncio.sleep is
        # cancellation-aware so operator shutdown still works mid-sleep.
        await asyncio.sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _R125_BACKOFF_MAX_S)
