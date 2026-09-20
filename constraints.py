"""
constraints.py — Step 3 of the pipeline: build the rules a filing's numbers
should obey.

Two independent sources, tagged with rule_source so downstream code (and
the report) can tell them apart:

  "filer-calc" — arithmetic the company itself declared, read straight out
  of the filing's calculation linkbase (e.g. "these expense lines sum to
  Operating Expenses"). Built by instantiate_filer_calc_constraints().

  "DQC"        — the official XBRL US Data Quality Committee rule set
  (sign checks, invalid dates, deprecated elements, and more), run via the
  xule Arelle plugin that setup_dqc.py installs. Built by
  build_dqc_constraints(), which shells out to Arelle as a subprocess
  because the DQC plugin is driven through Arelle's command line, not a
  documented in-process Python API.

Input: the loaded model (from extract.load_filing), the filing URL, and
the fact records / unique numeric facts (from extract.extract_facts /
extract.dedupe_numeric_facts). Output: a flat list of constraint dicts —
no pass/fail here, that's check.py's job. This file only decides WHAT
should be checked, not whether it holds.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from arelle import XbrlConst

USER_AGENT = "Temirlan Yerlanuly research tool (temirlan.eraly1@gmail.com)"

DQC_TIMEOUT_SECONDS = 900


def context_signature(context_ref: dict | None):
    """
    A hashable key for "this is the same real-world period/entity/dims",
    independent of the raw context_id string a filer happened to use.
    Two facts with the same signature can be compared/combined even if
    their literal contextRef attributes differ.
    """
    if context_ref is None:
        return None
    period_key = tuple(sorted(context_ref["period"].items()))
    dims_key = tuple(sorted(context_ref["dimensions"].items()))
    return (context_ref["entity"], period_key, dims_key)


def build_filer_calc_templates(model) -> dict:
    """
    Group the filing's calculation linkbase into concept-level templates:
    (link role, total concept) -> [(child concept, weight), ...]

    This is concept-level, not fact-level — the same total concept can
    have many facts (one per period/dimension), each of which gets
    instantiated separately in instantiate_filer_calc_constraints().
    """
    calc_rels = model.relationshipSet(XbrlConst.summationItem)

    templates: dict = {}
    for rel in calc_rels.modelRelationships:
        total_concept = rel.fromModelObject
        child_concept = rel.toModelObject
        if total_concept is None or child_concept is None:
            continue
        key = (rel.linkrole, str(total_concept.qname))
        templates.setdefault(key, []).append((str(child_concept.qname), rel.weight))

    return templates


def instantiate_filer_calc_constraints(templates: dict, unique_numeric_facts: list) -> tuple:
    """
    For every (link role, total concept) template, find each context where
    the total concept has a fact, then look for matching facts for every
    child concept at that same real-world context (same period/entity/dims).
    If any child is missing, the constraint can't be instantiated at that
    context. As in XBRL calculation semantics, the constraint sums whichever
    children ARE reported (a missing child is treated as absent, not as a
    reason to skip) and is marked partial=True with the missing concepts
    listed. Only when none of the children are reported is the attempt
    recorded as a skipped failure (an incomplete-processing gap, not an
    inconsistency) rather than silently dropped. Nil facts (value is None — the filer explicitly reported "no value")
    are excluded from calculation entirely, same as if they were absent.

    Returns (constraints, skipped) where skipped is a list of dicts
    describing each total-fact/template pair that couldn't be instantiated.
    """
    non_nil_facts = [f for f in unique_numeric_facts if f["value"] is not None]

    facts_by_signature_concept: dict = {}
    for fact in non_nil_facts:
        sig = context_signature(fact["context_ref"])
        facts_by_signature_concept.setdefault((sig, fact["concept"]), []).append(fact)

    constraints = []
    skipped = []
    for (linkrole, total_qname), children in templates.items():
        total_facts = [f for f in non_nil_facts if f["concept"] == total_qname]

        for total_fact in total_facts:
            sig = context_signature(total_fact["context_ref"])

            component_facts = []
            weights = []
            missing_children = []
            for child_qname, weight in children:
                candidates = facts_by_signature_concept.get((sig, child_qname))
                if not candidates:
                    missing_children.append(child_qname)
                    continue
                match = next((c for c in candidates if c["unit"] == total_fact["unit"]), candidates[0])
                component_facts.append(match)
                weights.append(weight)

            if not component_facts:
                skipped.append({
                    "reason": "no_child_facts",
                    "linkrole": linkrole,
                    "total_concept": total_qname,
                    "total_fact_id": total_fact["fact_id"],
                    "missing_child_concepts": missing_children,
                })
                continue

            involved_fact_ids = [total_fact["fact_id"]] + [f["fact_id"] for f in component_facts]
            constraints.append({
                "constraint_id": f"CALC-{len(constraints)}-{total_qname}",
                "rule_source": "filer-calc",
                "kind": "sum",
                "linkrole": linkrole,
                "total_fact_id": total_fact["fact_id"],
                "component_fact_ids": [f["fact_id"] for f in component_facts],
                "weights": weights,
                "involved_fact_ids": involved_fact_ids,
                "partial": bool(missing_children),
                "missing_child_concepts": missing_children,
            })

    return constraints, skipped


def _parse_dqc_ref(ref: dict) -> dict:
    props = dict((p[0], p[1]) for p in ref.get("properties", []))
    return {
        "concept": props.get("QName"),
        "context_id": props.get("contextRef"),
        "object_id": ref.get("objectId"),
    }


def run_dqc_validation(url: str, timeout: int = DQC_TIMEOUT_SECONDS) -> tuple:
    """
    Run the real XBRL US DQC rule set (via the xule plugin setup_dqc.py
    installs) against a filing URL, as a subprocess. The DQC plugin only
    logs violations — a rule that finds nothing wrong is simply silent, so
    there is no "this rule passed" list to compare against, only observed
    violations.

    Returns (findings, status) where status distinguishes "ran cleanly and
    found N violations" from "didn't actually run" (timeout, crash, no log
    produced) — an empty findings list must never be confused with a
    failed run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "dqc_log.json"
        cmd = [
            sys.executable, "-m", "arelle.CntlrCmdLine",
            "--plugins", "validate/DQC",
            "--httpUserAgent", USER_AGENT,
            "-f", url,
            "--validate",
            "--logFile", str(log_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return [], {"ok": False, "reason": "timeout", "detail": f"DQC run exceeded {timeout}s"}

        if not log_path.exists():
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            return [], {"ok": False, "reason": "no_log_produced", "detail": detail}

        with open(log_path) as f:
            data = json.load(f)

    findings = []
    for entry in data.get("log", []):
        code = entry.get("code", "")
        if not code.startswith("DQC."):
            continue
        findings.append({
            "rule_code": code,
            "level": entry.get("level"),
            "message": entry.get("message", {}).get("text", ""),
            "refs": [_parse_dqc_ref(r) for r in entry.get("refs", [])],
        })

    return findings, {"ok": True}


def build_dqc_constraints(findings: list, records: list) -> list:
    """
    Turn raw DQC findings into constraint dicts, matching each finding's
    referenced fact(s) back to our extracted fact records by concept +
    raw context_id (both loads parse the identical source document, so raw
    context ids match even though this is a separate Arelle process from
    extract.py's).

    Every DQC constraint here represents an observed violation — there is
    no notion of a "passing" DQC constraint, since passing rules never
    appear in the log at all (see run_dqc_validation).
    """
    by_concept_context = {}
    for rec in records:
        ctx = rec.get("context_ref")
        if ctx is None:
            continue
        by_concept_context.setdefault((rec["concept"], ctx["context_id"]), []).append(rec)

    constraints = []
    for i, finding in enumerate(findings):
        involved_fact_ids = []
        for ref in finding["refs"]:
            matches = by_concept_context.get((ref["concept"], ref["context_id"]), [])
            involved_fact_ids.extend(m["fact_id"] for m in matches)

        constraints.append({
            "constraint_id": f"DQC-{i}-{finding['rule_code']}",
            "rule_source": "DQC",
            "kind": "dqc_rule",
            "rule_code": finding["rule_code"],
            "severity": finding["level"],
            "message": finding["message"],
            "involved_fact_ids": involved_fact_ids,
            "result": "FAIL",
        })

    return constraints


def compute_spans_sections(involved_fact_ids: list, fact_by_id: dict) -> bool:
    """
    A constraint "spans sections" only if there is NO single section
    containing all of its facts — not just because some fact also happens
    to appear elsewhere too. A single-fact constraint never spans sections.
    """
    if len(involved_fact_ids) <= 1:
        return False

    section_sets = []
    for fid in involved_fact_ids:
        fact = fact_by_id.get(fid)
        if fact is None:
            continue
        section_sets.append(set(fact.get("sections", [])))

    if not section_sets:
        return False

    intersection = set.intersection(*section_sets)
    return len(intersection) == 0


def build_constraints(model, url: str, records: list, unique_numeric_facts: list) -> dict:
    """
    Build every constraint for a filing: filer-calc (from the model already
    loaded in memory) + DQC (via a fresh Arelle subprocess against the same
    URL). Tags every constraint with spans_sections.

    Returns {"constraints": [...], "failures": [...]} — failures covers
    both filer-calc templates that couldn't be instantiated (missing
    facts) and a DQC run that didn't complete cleanly, so a report can
    tell "the pipeline had a gap here" apart from "the filing is wrong."
    """
    fact_by_id = {f["fact_id"]: f for f in unique_numeric_facts}

    calc_templates = build_filer_calc_templates(model)
    calc_constraints, calc_skipped = instantiate_filer_calc_constraints(calc_templates, unique_numeric_facts)

    dqc_findings, dqc_status = run_dqc_validation(url)
    dqc_constraints = build_dqc_constraints(dqc_findings, records)

    all_constraints = calc_constraints + dqc_constraints
    for constraint in all_constraints:
        constraint["spans_sections"] = compute_spans_sections(constraint["involved_fact_ids"], fact_by_id)

    failures = [{"stage": "filer-calc", **skip} for skip in calc_skipped]
    if not dqc_status["ok"]:
        failures.append({"stage": "DQC", **dqc_status})

    return {"constraints": all_constraints, "failures": failures}


if __name__ == "__main__":
    import extract

    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
    )

    print(f"Loading {url} ...")
    model = extract.load_filing(url)
    records = extract.extract_facts(model)
    unique_numeric, counts = extract.dedupe_numeric_facts(records)
    print("extraction counts:", counts)

    print("\nBuilding filer-calc constraints ...")
    templates = build_filer_calc_templates(model)
    print(f"calc templates (total-concept groups): {len(templates)}")
    calc_constraints, calc_skipped = instantiate_filer_calc_constraints(templates, unique_numeric)
    print(f"instantiated filer-calc constraints: {len(calc_constraints)}")
    print(f"skipped (missing child fact): {len(calc_skipped)}")
    if calc_constraints:
        print("sample:", calc_constraints[0])

    print("\nRunning DQC validation (this takes a few minutes) ...")
    findings, dqc_status = run_dqc_validation(url)
    print(f"DQC status: {dqc_status}")
    print(f"DQC findings: {len(findings)}")
    dqc_constraints = build_dqc_constraints(findings, records)
    for c in dqc_constraints:
        print(" -", c["rule_code"], "| involved_fact_ids:", c["involved_fact_ids"])

    fact_by_id = {f["fact_id"]: f for f in unique_numeric}
    spans = sum(1 for c in calc_constraints if compute_spans_sections(c["involved_fact_ids"], fact_by_id))
    print(f"\nfiler-calc constraints spanning sections: {spans} / {len(calc_constraints)}")
