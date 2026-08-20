---
title: AI agents
description: The specialized agents that make up ARTA's pipeline — strategy, ATDD design, automation engineering, grounding validation, defect intelligence, and self-healing.
---

ARTA is not one prompt in a loop. It is a pipeline of specialized agents —
plain Python classes under
[`src/agents/`](https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/tree/main/src/agents)
— each owning one responsibility, with deterministic validators between the
LLM and anything that ships.

## The core pipeline agents

| Agent | Module | Responsibility |
| --- | --- | --- |
| Orchestrator | `orchestrator.py` | Coordinates the pipeline end to end |
| Strategy architect | `strategy_architect.py` | Requirement analysis and risk scoring (probability × impact) |
| Requirement decomposer | `requirement_decomposer.py` | Splits requirements into testable units |
| ATDD designer | `atdd_designer.py` | Gherkin acceptance criteria per requirement |
| Automation engineer | `automation_engineer.py` | Generates test scripts per runtime, grounded in discovery context |
| Grounding validator | `grounding_validator.py` | Rejects hallucinated selectors, roles, and endpoints at generation time |
| Execution agent | `execution_agent.py` | Drives test execution |
| Defect intelligence | `defect_intel.py` | Classifies failures (`sut_regression` / `test_gen_bug` / `grounding_blocked`) and files defects |
| Quality gate | `quality_gate_agent.py` | Aggregates results into a pass/fail gate |
| Self-healing | `self_healing.py` | Consumes the regeneration queue; retries failed generation with corrective hints |

## Discovery and understanding agents

| Agent | Module | Responsibility |
| --- | --- | --- |
| API discovery | `api_discovery.py` | Captures real endpoints from the running SUT |
| Architecture discovery | `architecture_discovery.py` | Builds a picture of the SUT's structure |
| Discovery executor | `discovery_executor.py` | Runs the read-only discovery probe |
| Protocol discovery | `protocol_discovery.py` | Classifies endpoint protocols beyond plain REST |
| Endpoint grounding | `endpoint_grounding.py` | Maintains the store of verified-real endpoints |
| GitHub context | `github_context.py` | Pulls route/DTO context from the SUT's source repository |
| DTO extractor | `dto_extractor.py` | Derives request-body shapes from SUT source |

## Supporting machinery

Around the agents sit deliberately boring, deterministic pieces: retry
ladders and retry policies, a circuit breaker, LLM client abstractions (the
Ollama client and cloud-provider clients), traceability agents that write the
graph, and evidence collectors that keep reports auditable. The LLM proposes;
deterministic code disposes.

The design position: **an agent may be creative only inside boundaries a
validator can check.** That is what makes the difference between "AI wrote my
tests" and "AI wrote my tests, and here is the proof they are real."
