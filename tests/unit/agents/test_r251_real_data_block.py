"""R251 (WS1b) — the [HARD CONSTRAINT — REAL TEST DATA] prompt block.

ARTA grounded endpoints/selectors/auth against the real SUT, then told the LLM
to "use real data values (not <placeholders>)" while handing it no real values.
Invention was the only option left. These tests pin the constraint AND its
budget: the documented 52.9%→38.6% regression came from prompt bloat, so a
grounding block that grows without bound trades one failure mode for another.
"""
import pytest

from src.agents import real_id_store as ris
from src.agents.automation_engineer import AutomationEngineerAgent


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ris, "_REAL_IDS_DIR", tmp_path / "ids")
    monkeypatch.delenv("ARTA_R250_REAL_ID_STORE_DISABLE", raising=False)
    monkeypatch.delenv("ARTA_R251_REAL_DATA_BLOCK_DISABLE", raising=False)


@pytest.fixture
def agent():
    return AutomationEngineerAgent(client=None)


def _seed(pid="pid-1", **entities):
    recs = [
        {"method": "GET", "path": f"/api/v1/{name}s", "status": 200,
         "response_body_sample": {"data": [{f"{name}_id": v} for v in vals]}}
        for name, vals in entities.items()
    ]
    ris.persist_real_ids(pid, ris.extract_real_ids(recs))


def test_r251_emits_real_ids_for_gherkin_entity(agent):
    _seed(account=["ACC-9F31A2", "ACC-7B0C11"])
    block = agent._r251_real_data_block("pid-1", "Given an account exists")
    assert "[HARD CONSTRAINT — REAL TEST DATA]" in block
    assert "ACC-9F31A2" in block
    assert "NEVER invent an id" in block


def test_r251_teaches_the_var_escape_hatch(agent):
    """The contract's other half: unlisted entity => {{var}}, not invention."""
    _seed(account=["ACC-1"])
    block = agent._r251_real_data_block("pid-1", "Given an account exists")
    assert "{{entity_id}}" in block
    assert "BLOCK" in block


def test_r251_empty_store_emits_nothing(agent):
    """No store => keep the pre-R251 prompt rather than an empty constraint."""
    assert agent._r251_real_data_block("pid-none", "Given an account") == ""


def test_r251_no_project_id_emits_nothing(agent):
    assert agent._r251_real_data_block("", "Given an account") == ""


def test_r251_killswitch(agent, monkeypatch):
    _seed(account=["ACC-1"])
    monkeypatch.setenv("ARTA_R251_REAL_DATA_BLOCK_DISABLE", "1")
    assert agent._r251_real_data_block("pid-1", "Given an account") == ""


def test_r251_filters_by_gherkin_relevance(agent):
    """An entity the scenario never mentions is bloat."""
    _seed(account=["ACC-1"], widget=["W-1"])
    block = agent._r251_real_data_block("pid-1", "Given an account exists")
    assert "ACC-1" in block
    assert "W-1" not in block


def test_r251_respects_budget(agent):
    """Bloat is what regressed the suite; the block must stay small even with a
    large store."""
    _seed(**{f"entity{i}": [f"E{i}-{j}" for j in range(10)] for i in range(20)})
    block = agent._r251_real_data_block("pid-1", "Given something happens")
    assert len(block) <= 1200, f"R251 block too large: {len(block)} chars"


def test_r251_caps_values_per_entity(agent):
    _seed(account=[f"ACC-{i}" for i in range(10)])
    block = agent._r251_real_data_block("pid-1", "Given an account exists")
    assert block.count("ACC-") <= 3


def test_r251_ollama_compression(agent, monkeypatch):
    _seed(**{f"e{i}": [f"V{i}"] for i in range(12)})
    monkeypatch.setattr(
        agent, "_r126_a_should_include", lambda name: "compress")
    block = agent._r251_real_data_block("pid-1", "Given something happens")
    # top-6 entities under compression
    assert sum(1 for ln in block.splitlines() if ln.strip().startswith("e")) <= 6


def test_r251_store_disabled_emits_nothing(agent, monkeypatch):
    _seed(account=["ACC-1"])
    monkeypatch.setenv("ARTA_R250_REAL_ID_STORE_DISABLE", "1")
    assert agent._r251_real_data_block("pid-1", "Given an account") == ""


# ── the prompt-rule flips (the two invention licenses) ───────────────────────

def test_tea_prompts_no_longer_licenses_invented_happy_path_data():
    from src.prompts import tea_prompts
    src = tea_prompts.GHERKIN_GENERATION
    assert "use real data values (not <placeholders>)" not in src
    assert "NEVER invent an id" in src


def test_tea_prompts_scopes_random_uuid_to_creates():
    """`crypto.randomUUID()` is right on a create and catastrophic on a GET."""
    from src.prompts import tea_prompts
    for src in (tea_prompts.TEST_GENERATION_QUALITY_RULES,
                tea_prompts.TEST_GENERATION_QUALITY_RULES_COMPACT):
        assert "CREATES" in src
        assert "404" in src
