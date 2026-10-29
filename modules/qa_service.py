"""Q&A pipeline — deterministic rules engine first, configured LLM as fallback.

The pipeline yields plain ``dict`` events (``meta`` / ``token`` / ``done`` /
``error``); the HTTP layer (main.py) only serialises them to SSE. Keeping the
event model transport-free makes the whole flow unit-testable without a server.

Event sequence for a request:

* rules hit       → meta(rules, matched) → token(full answer) → done
* rules-mode miss → meta(rules, no match) → token(guidance) → done(suggestions)
* AI fallback     → meta(ai) → token* (proxied from the LLM) → done(subgraph)
* failure         → error (no configured model, upstream LLM error)

The caller supplies a ``log_call`` callback (so the generator stays
connection-free); it receives the same kwargs as
:func:`modules.llm_client.log_call`.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from modules import deep_analysis as deep
from modules import llm_client as llm
from modules import ontology_service as svc

NO_MATCH_MESSAGE = (
    "**No matching ontology rule.** The deterministic rules engine "
    "has no canonical answer for this question yet. Try one of the "
    "suggested questions below, or switch to **AI mode** for a "
    "model-generated answer grounded in the same knowledge base."
)


def rule_answer(
    question: str,
    pack,
    computed,
    history: Optional[List[Dict[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    """Ask the rules engine; ``None`` when no rule matches."""
    # Live Q&A: a rule whose template cannot render (e.g. missing input
    # rows) is skipped so Auto can fall back to AI / suggestions.
    res = svc.answer_question(
        question, pack, computed, raise_on_template_error=False, history=history
    )
    if res is None:
        return None
    sub = svc.retrieve_subgraph(
        pack,
        question,
        res["answer"],
        (res.get("nodes") or []) + ((res.get("path") or {}).get("steps") or []),
    )
    return {
        "answer": res["answer"],
        "category": res["category"],
        "nodes": res.get("nodes", []),
        "path": res.get("path"),
        "graph": sub,
    }


def suggestions(question: str, pack) -> List[Dict[str, str]]:
    """Top similar bank questions, normalised to {question, category}."""
    items = svc.similar_questions(question, 3, pack.get("id"))
    return [
        (
            {"question": s["question"], "category": s.get("category", "")}
            if isinstance(s, dict)
            else {"question": s, "category": ""}
        )
        for s in items
    ]


async def stream_answer(
    question: str,
    mode: str,
    pack: Dict[str, Any],
    computed: Dict[str, Any],
    model_cfg: Optional[Dict[str, Any]],
    *,
    history: Optional[List[Dict[str, str]]] = None,
    user_id: Optional[int] = None,
    logger=None,
    log_call: Optional[Callable[..., None]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Full answer pipeline for one question, as an async event stream."""
    analysis = deep.analyze(pack, computed, question, history=history)
    if mode in ("auto", "rules"):
        hit = rule_answer(question, pack, computed, history=history)
        if hit:
            if logger:
                logger.info(f"[rules:{hit['category']}] {question[:80]}")
            answer = hit["answer"]
            if analysis.get("available") and analysis.get("body"):
                answer = answer + "\n\n" + analysis["body"]
            yield {
                "type": "meta",
                "source": "rules",
                "matched": True,
                "category": hit["category"],
                "graph": hit["graph"],
                "analysis": _analysis_payload(analysis),
            }
            yield {"type": "token", "content": answer}
            yield {
                "type": "done",
                "source": "rules",
                "matched": True,
                "category": hit["category"],
                "graph": hit["graph"],
                "analysis": _analysis_payload(analysis),
                "latency_ms": 0,
                "model": "ontology-engine",
            }
            return

    sugg = suggestions(question, pack)
    if mode == "rules":
        yield {
            "type": "meta",
            "source": "rules",
            "matched": False,
            "category": "No rule matched",
            "suggestions": sugg,
            "graph": None,
        }
        yield {"type": "token", "content": NO_MATCH_MESSAGE}
        yield {
            "type": "done",
            "source": "rules",
            "matched": False,
            "suggestions": sugg,
            "graph": None,
            "latency_ms": 0,
            "model": "ontology-engine",
        }
        return

    if model_cfg is None:
        yield {
            "type": "error",
            "source": "ai",
            "error": "No enabled AI model is configured yet. You can still "
            "use Rules mode, or ask an administrator to add a model.",
        }
        return

    client = llm.ConfiguredLLM(model_cfg, logger=logger)
    if analysis.get("available"):
        # Dual-engine mode: the deterministic analysis supplies every number,
        # the model only narrates the pre-computed reasoning chain.
        system = llm.build_synthesis_prompt(
            analysis["full"], svc.build_llm_context(pack)
        )
    else:
        system = llm.build_system_prompt(svc.build_llm_context(pack))
    yield {
        "type": "meta",
        "source": "ai",
        "matched": True,
        "category": "AI answer",
        "model": model_cfg.get("model"),
        "graph": None,
        "analysis": _analysis_payload(analysis),
    }
    collected, t0, ok, tokens = "", time.time(), False, 0
    try:
        async for chunk in client.answer_stream(system, question, history):
            if not chunk.startswith("data: "):
                continue
            evt = json.loads(chunk[6:].strip())
            etype = evt.get("type")
            if etype == "token":
                collected += evt.get("content", "")
                yield evt
            elif etype == "error":
                yield evt
            elif etype == "done":
                ok = True
                tokens = int(evt.get("tokens") or 0)
                sub = svc.retrieve_subgraph(pack, question, collected)
                yield {
                    "type": "done",
                    "source": "ai",
                    "matched": True,
                    "graph": sub,
                    "latency_ms": round((time.time() - t0) * 1000, 2),
                    "model": evt.get("model", model_cfg.get("model")),
                    "tokens": tokens,
                }
    finally:
        if log_call:
            log_call(
                user_id=user_id,
                config_id=model_cfg.get("id"),
                model=model_cfg.get("model", "?"),
                success=ok,
                latency_ms=round((time.time() - t0) * 1000, 2),
                tokens=tokens,
            )


# --------------------------------------------------------------------------- #
# Side-by-side comparison: answer WITH the ontology vs. WITHOUT it
# --------------------------------------------------------------------------- #
NO_MODEL_COMPARE_MESSAGE = (
    "No enabled AI model is configured, so the non-ontology answer cannot be "
    "generated. The ontology-grounded answer above still works — the rules "
    "engine needs no model. Ask an administrator to add a model in Model "
    "centre to run both sides."
)


def _analysis_payload(analysis: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact, JSON-safe view of the deep analysis for the UI / API."""
    if not analysis or not analysis.get("available"):
        return {"available": False}
    return {
        "available": True,
        "headline": analysis.get("headline", ""),
        "month": analysis.get("month"),
        "focus": (analysis.get("intent") or {}).get("focus"),
        "steps": analysis.get("steps", []),
        "findings": analysis.get("findings", []),
        "stats": analysis.get("stats", []),
        "depth": analysis.get("depth", {}),
    }


def _grounding(
    computed: Dict[str, Any], graph: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """What the ontology contributed to an answer, for the comparison panel."""
    tables = sorted((computed.get("tables") or {}).keys())
    metrics = sorted((computed.get("metrics") or {}).keys())
    nodes = (graph or {}).get("nodes") or []
    edges = (graph or {}).get("edge_ids") or []
    return {
        "tables": tables,
        "metrics": metrics,
        "table_count": len(tables),
        "metric_count": len(metrics),
        "graph_nodes": len(nodes),
        "graph_edges": len(edges),
    }


def compare_answers(
    question: str,
    mode: str,
    pack: Dict[str, Any],
    computed: Dict[str, Any],
    model_cfg: Optional[Dict[str, Any]],
    *,
    user_id: Optional[int] = None,
    history: Optional[List[Dict[str, str]]] = None,
    logger=None,
    log_call: Optional[Callable[..., None]] = None,
) -> Dict[str, Any]:
    """Answer one question twice: grounded in the ontology, and by the same
    model with no ontology context at all.

    Deliberately synchronous (two blocking model calls) so the API layer can
    run it in a worker thread; the ontology side stays on the rules engine when
    a rule matches, which needs no model.
    """
    analysis = deep.analyze(pack, computed, question, history=history)

    with_onto: Dict[str, Any] = {
        "label": "With ontology",
        "source": "none",
        "answer": "",
        "graph": None,
        "model": None,
        "grounding": _grounding(computed, None),
        "analysis": _analysis_payload(analysis),
        "narrative": "",
    }

    hit = (
        rule_answer(question, pack, computed, history=history)
        if mode in ("auto", "rules")
        else None
    )

    blocks: List[str] = []
    if hit:
        blocks.append(hit["answer"])
    if analysis.get("available") and analysis.get("body"):
        # With a rule hit the headline is already in the rule answer; without
        # one, the analysis' own headline leads the answer.
        blocks.append(analysis["body"] if hit else analysis["full"])

    if blocks:
        graph = svc.retrieve_subgraph(pack, question, "\n".join(blocks))
        source = (
            "rules+analysis"
            if (hit and analysis.get("available"))
            else ("rules" if hit else "analysis")
        )
        with_onto.update(
            source=source,
            answer="\n\n".join(blocks),
            graph=graph,
            model="ontology-engine",
            grounding=_grounding(computed, graph),
        )
    elif model_cfg is not None:
        client = llm.ConfiguredLLM(model_cfg, logger=logger)
        res = client.answer(
            llm.build_system_prompt(svc.build_llm_context(pack)), question, history
        )
        if res.get("status") == "success":
            text = res.get("response", "")
            graph = svc.retrieve_subgraph(pack, question, text)
            with_onto.update(
                source="ai",
                answer=text,
                graph=graph,
                model=res.get("model") or model_cfg.get("model"),
                grounding=_grounding(computed, graph),
            )
        else:
            with_onto.update(source="error", answer=res.get("error", "Model error."))
    else:
        with_onto.update(
            source="none",
            answer="No ontology rule matched, and no AI model is configured to "
            "answer with the ontology. Try one of the suggested questions below.",
        )

    # Second wheel: the model narrates the pre-computed analysis. This is the
    # "Rule engine + LLM" joint mode — it never recomputes the numbers, so the
    # deterministic evidence above stays authoritative.
    if model_cfg is not None and analysis.get("available"):
        client = llm.ConfiguredLLM(model_cfg, logger=logger)
        sys_prompt = llm.build_synthesis_prompt(
            analysis["full"],
            svc.build_llm_context(pack),
            rules_answer=hit["answer"] if hit else "",
        )
        nres = client.answer(sys_prompt, question, history)
        if nres.get("status") == "success":
            with_onto["narrative"] = nres.get("response", "")
        if log_call:
            log_call(
                user_id=user_id,
                config_id=model_cfg.get("id"),
                model=model_cfg.get("model", "?"),
                success=nres.get("status") == "success",
                latency_ms=nres.get("latency_ms", 0),
                tokens=int(nres.get("tokens_used") or 0),
            )

    # Count tables with actual data so the "without ontology" side can
    # report how many data tables it did use (no ontology entities/metrics
    # though — hence 0 graph nodes/edges).
    all_tables = computed.get("tables") or {}
    baseline_table_keys = sorted(k for k, rows in all_tables.items() if rows)
    baseline_table_names: List[str] = []
    for t in pack.get("tables", []):
        if t.get("key") in baseline_table_keys:
            baseline_table_names.append(t.get("name") or t.get("key", ""))

    baseline_grounding = {
        "tables": baseline_table_names,
        "metrics": [],  # baseline has no ontology metric definitions
        "table_count": len(baseline_table_names),
        "metric_count": 0,
        "graph_nodes": 0,
        "graph_edges": 0,
    }

    without: Dict[str, Any] = {
        "label": "Without ontology",
        "source": "unavailable" if model_cfg is None else "ai",
        "answer": "",
        "graph": None,
        "model": None,
        "grounding": baseline_grounding,
    }
    if model_cfg is None:
        without["answer"] = NO_MODEL_COMPARE_MESSAGE
    else:
        client = llm.ConfiguredLLM(model_cfg, logger=logger)
        # Ground the baseline side in the pack's uploaded data tables so it
        # can still reason over company figures — it only misses the ontology
        # layer (entities, rules, graph, formulas). This is what the side-by-
        # side comparison is designed to demonstrate: rules + graph inference
        # on top of data-grounded answers.
        baseline_ctx = svc.build_table_profiles(pack)
        res = client.answer(llm.build_baseline_prompt(baseline_ctx), question, history)
        if res.get("status") == "success":
            without.update(
                answer=res.get("response", ""),
                model=res.get("model") or model_cfg.get("model"),
                source="ai",
            )
        else:
            without.update(answer=res.get("error", "Model error."), source="error")
        if log_call:
            log_call(
                user_id=user_id,
                config_id=model_cfg.get("id"),
                model=model_cfg.get("model", "?"),
                success=res.get("status") == "success",
                latency_ms=res.get("latency_ms", 0),
                tokens=int(res.get("tokens_used") or 0),
            )

    depth = analysis.get("depth") or {}
    return {
        "question": question,
        "with_ontology": with_onto,
        "without_ontology": without,
        "value": {
            "tables_used": with_onto["grounding"]["table_count"],
            "metrics_used": with_onto["grounding"]["metric_count"],
            "chain_nodes": with_onto["grounding"]["graph_nodes"],
            "chain_edges": with_onto["grounding"]["graph_edges"],
            "baseline_tables_used": baseline_grounding["table_count"],
            "baseline_chain_nodes": 0,  # baseline has no impact chain
            # Reasoning-depth counters produced by the deterministic layer.
            "reasoning_steps": depth.get("reasoning_steps", 0),
            "stat_tests": depth.get("stat_tests", 0),
            "findings": depth.get("facts", 0),
            "tables_joined": depth.get("tables_joined", 0),
            "dimensions": depth.get("dimensions", 0),
            "reconciled": depth.get("reconciled", False),
            "ontology_chars": depth.get("chars", 0),
            "baseline_chars": len(without.get("answer") or ""),
        },
    }
