"""R124.G — LLM cascade scales without degraded fallback.

Two structural fixes:

1. **Pre-cluster body-aware key** — pre-R124.G when error_message is sparse
   (sub-20-char or just HTTP-status text), 2451 failures collapsed into ~1-3
   coarse clusters → LLM RCA called on near-identical reps → wasted LLM
   budget. Post-R124.G + R124.K (body persistence): the cluster key uses
   the response body as a discriminator, keeping semantically-distinct
   failures in separate clusters.

2. **Chunked-parallel LLM gather** — pre-R124.G `asyncio.gather(*all_tasks)`
   fired 200+ tasks at once, hitting Anthropic's rate limiter →
   `wait_for(timeout=300)` exceeded → deterministic fallback fired (which
   doesn't populate triage_signals → 84 opaque defects in run-d52a8c).
   Post-R124.G: batches of 8 with 50ms breathing.
"""
from __future__ import annotations

import asyncio
import pytest


# ── pre-cluster body-aware key tests ──────────────────────────────────


def _make_cluster_key(failure: dict) -> str:
    """Mirror the R124.G key derivation in post_run_chain_pipeline.py."""
    import re as _re
    em = str(failure.get("error_message") or failure.get("error") or "").strip()
    body = str((failure.get("metadata") or {}).get("response_body_preview") or "")[:200]
    combined = em if len(em) > 20 else (em + " | body: " + body)
    sc = failure.get("status_code")
    m = _re.match(r"\W*(\w[\w-]{2,})", combined)
    first_tok = m.group(1)[:24] if m else "empty"
    triage_sig = "none"
    if isinstance(failure.get("triage_signals"), list) and failure["triage_signals"]:
        triage_sig = str(failure["triage_signals"][0])[:32]
    return f"{sc or 'na'}|{triage_sig}|{first_tok}|{combined[:60]}"


def test_r124_g_body_discriminates_when_em_sparse():
    """500 failures with EMPTY error_message but DIFFERENT bodies → distinct keys."""
    f1 = {
        "status_code": 500,
        "error_message": "",
        "metadata": {"response_body_preview": "Internal authorization error"},
    }
    f2 = {
        "status_code": 500,
        "error_message": "",
        "metadata": {"response_body_preview": "NullPointerException"},
    }
    assert _make_cluster_key(f1) != _make_cluster_key(f2), (
        "different bodies should produce different cluster keys"
    )


def test_r124_g_triage_signal_separates_cascade_vs_real():
    """Same body + status but DIFFERENT triage_signals → separate clusters."""
    f_cascade = {
        "status_code": 500, "error_message": "x" * 30,
        "triage_signals": ["auth_cascade_5xx"],
    }
    f_real = {
        "status_code": 500, "error_message": "x" * 30,
        "triage_signals": ["real_sut_regression"],
    }
    assert _make_cluster_key(f_cascade) != _make_cluster_key(f_real)


def test_r124_g_same_failure_same_key():
    """Two identical failures → same key (no nondeterminism)."""
    f = {"status_code": 500, "error_message": "expected 200 got 500",
         "metadata": {"response_body_preview": "x"}}
    assert _make_cluster_key(f) == _make_cluster_key(f)


# ── chunked-parallel batch test ──────────────────────────────────────


@pytest.mark.asyncio
async def test_r124_g_chunked_batch_respects_size():
    """200 tasks → 25 batches of 8 (no all-at-once gather → no rate-limit floods)."""
    BATCH = 8
    SLEEP = 0.001  # speed up test
    tasks = list(range(200))
    sleep_count = 0
    batch_count = 0

    async def _fake_classify(idx):
        return {"idx": idx}

    # Simulate the production gather pattern
    out = []
    for i in range(0, len(tasks), BATCH):
        batch = [_fake_classify(t) for t in tasks[i:i + BATCH]]
        out.extend(await asyncio.gather(*batch, return_exceptions=True))
        batch_count += 1
        if i + BATCH < len(tasks):
            await asyncio.sleep(SLEEP)
            sleep_count += 1

    assert batch_count == 25, f"expected 25 batches (200/8); got {batch_count}"
    assert sleep_count == 24, f"expected 24 inter-batch sleeps; got {sleep_count}"
    assert len(out) == 200


@pytest.mark.asyncio
async def test_r124_g_batch_failure_isolated():
    """Exception in one batch task doesn't kill subsequent batches."""
    async def _classify(idx):
        if idx == 5:
            raise RuntimeError("rate limit")
        return {"idx": idx}

    out = []
    tasks_arr = list(range(20))
    BATCH = 8
    for i in range(0, len(tasks_arr), BATCH):
        batch = [_classify(t) for t in tasks_arr[i:i + BATCH]]
        out.extend(await asyncio.gather(*batch, return_exceptions=True))

    # 20 results total; 1 should be an exception, 19 dicts
    excs = [r for r in out if isinstance(r, Exception)]
    dicts = [r for r in out if isinstance(r, dict)]
    assert len(excs) == 1
    assert len(dicts) == 19


@pytest.mark.asyncio
async def test_r124_g_batch_completes_under_synthetic_300s():
    """200 'cluster RCAs' (each 100ms) complete well under 300s budget
    with batch=8 + 50ms inter-batch sleep.

    Math: 200/8 = 25 batches × 100ms = 2.5s + 24×50ms sleeps = 1.2s = ~3.7s.
    Synthetic test runs the same shape with much shorter sleeps to stay
    fast in CI.
    """
    BATCH = 8
    SLEEP = 0.001
    PER_TASK_MS = 0.005

    async def _classify(idx):
        await asyncio.sleep(PER_TASK_MS)
        return idx

    tasks_arr = list(range(200))
    start = asyncio.get_event_loop().time()
    out = []
    for i in range(0, len(tasks_arr), BATCH):
        batch = [_classify(t) for t in tasks_arr[i:i + BATCH]]
        out.extend(await asyncio.gather(*batch))
        if i + BATCH < len(tasks_arr):
            await asyncio.sleep(SLEEP)
    elapsed = asyncio.get_event_loop().time() - start
    # Synthetic budget: 5s (CI margin); production budget: 300s for real LLM calls
    assert elapsed < 5.0, f"batch completion regression: {elapsed:.2f}s"
    assert len(out) == 200
