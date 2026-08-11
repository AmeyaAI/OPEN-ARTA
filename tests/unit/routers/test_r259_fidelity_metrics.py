"""R259 — ARTA reporting-fidelity metrics.

Pre-R259 nothing measured ARTA's OWN defect rate: run-0c19e6 reported 520
`sut_regression` defects when only ~9 failures were genuine SUT signal, and
Pillar 4 graded that CLEAN — indistinguishable from a truly healthy SUT.
"""
from src.api.routers.execution import _r259_fidelity_metrics


def test_r259_empty_run_is_zeros_not_none():
    """Callers compare numerically; `triaged == 0` marks 'nothing to report'."""
    out = _r259_fidelity_metrics([])
    assert out["triaged"] == 0
    assert out["arta_defect_rate"] == 0.0
    assert out["noise_signal_ratio"] == 0.0


def test_r259_run_0c19e6_shape_is_mostly_noise():
    """The definitive run: ~520 ARTA-attributed vs ~9 genuine SUT signals."""
    out = _r259_fidelity_metrics([
        ("test_gen_bug", 0.85, 520),
        ("sut_regression", 0.90, 9),
    ])
    assert out["arta_attributed"] == 520
    assert out["sut_signal_count"] == 9
    assert out["arta_defect_rate"] > 0.9
    assert out["noise_signal_ratio"] > 50


def test_r259_low_confidence_sut_defect_is_unknown_not_signal():
    """A low-confidence accusation is not evidence against the SUT."""
    out = _r259_fidelity_metrics([("sut_regression", 0.4, 10)])
    assert out["sut_signal_count"] == 0
    assert out["unknown"] == 10
    assert out["not_assessed_pct"] == 1.0


def test_r259_confidence_threshold_boundary():
    """0.7 is signal; just below is not."""
    assert _r259_fidelity_metrics([("sut_regression", 0.7, 5)])["sut_signal_count"] == 5
    assert _r259_fidelity_metrics([("sut_regression", 0.69, 5)])["sut_signal_count"] == 0


def test_r259_not_assessed_is_unknown_but_named_opreview_is_adjudication():
    """R304 — `not_assessed` / unclassified operator_review (conf 0.0) is truly
    unknown, but a NAMED operator_review (conf >= 0.7: auth_scope_mismatch,
    sut_query_or_validation_contract) is ASSESSED-pending-adjudication, a
    distinct bucket — it was wrongly lumped into `unknown` pre-R304 and drove
    the inflated not_assessed_pct."""
    out = _r259_fidelity_metrics([
        ("not_assessed", 0.0, 4),
        ("operator_review", 0.0, 3),   # LAYER-3 fallback → still unknown
        ("operator_review", 0.8, 6),   # named/evidence-backed → adjudication
    ])
    assert out["unknown"] == 7
    assert out["adjudication_pending"] == 6
    assert out["not_assessed_pct"] == round(7 / 13, 4)
    assert out["adjudication_pending_pct"] == round(6 / 13, 4)
    assert out["sut_signal_count"] == 0
    assert out["arta_attributed"] == 0


def test_r259_grounding_blocked_counts_as_arta_attributed():
    """R118.G's grounding_blocked is ARTA's own gen defect, not the SUT's."""
    out = _r259_fidelity_metrics([("grounding_blocked", 0.9, 7)])
    assert out["arta_attributed"] == 7
    assert out["arta_defect_rate"] == 1.0


def test_r259_healthy_run_has_low_noise():
    """The target state: mostly real SUT signal, little ARTA noise."""
    out = _r259_fidelity_metrics([
        ("sut_regression", 0.9, 40),
        ("test_gen_bug", 0.85, 2),
    ])
    assert out["arta_defect_rate"] < 0.05
    assert out["noise_signal_ratio"] == 0.05


def test_r259_null_confidence_is_not_signal():
    """A NULL triage_confidence cannot clear the bar."""
    out = _r259_fidelity_metrics([("sut_regression", None, 3)])
    assert out["sut_signal_count"] == 0
    assert out["unknown"] == 3


def test_r259_ignores_zero_and_malformed_counts():
    out = _r259_fidelity_metrics([
        ("test_gen_bug", 0.85, 0),
        ("sut_regression", 0.9, None),
        ("test_gen_bug", 0.85, 5),
    ])
    assert out["triaged"] == 5
    assert out["arta_attributed"] == 5


def test_r259_noise_ratio_when_no_signal_is_unbounded():
    """'12 noise, 0 signal' must not round to a flattering ceiling."""
    out = _r259_fidelity_metrics([("test_gen_bug", 0.85, 12)])
    assert out["noise_signal_ratio"] == 12.0
    assert out["sut_signal_count"] == 0
