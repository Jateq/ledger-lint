"""
inject.py — fault injection: make a provably-bad copy of a provably-clean filing.

Pipeline for one filing:
  1. Load the clean filing, build filer-calc constraints, check them.
  2. solver.py must say SAT (otherwise the filing is discarded).
  3. Pick a fact that sits in >= 2 complete constraints and raise its
     magnitude by just enough to leave the tightest constraint's rounding
     band (v1: a fixed offset, not random).
  4. Re-run Z3 on the modified values — it MUST say UNSAT; the unsat core is
     the ground-truth "where it is bad".
  5. Splice the new number into a byte-for-byte copy of the original .htm,
     changing only the text inside that fact's ix:nonFraction tag(s).
  6. Verify by re-parsing the written file with Arelle and re-running Z3
     (and, as a control, re-parsing the untouched copy, which must be SAT).

Output per filing, in datasets/<TICKER>/<accession>/ : good.htm, _support/,
for_llm/case_NN.htm, answers/case_NN.{txt,json}, summary.json (see inject()).
`--count N` makes N cases, each changing a different fact.
"""

import copy
import json
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import check
import constraints as constraints_mod
import extract
import fetch
import ratios
import solver

DATASETS_DIR = Path("datasets")
NUM_FORMAT = b"ixt:num-dot-decimal"
PLAIN_NUMBER = re.compile(r"^[\d,]+(\.\d+)?$")


def _attr(open_tag: bytes, name: bytes):
    m = re.search(rb'\s' + name + rb'="([^"]*)"', open_tag)
    return m.group(1) if m else None


def find_element(html: bytes, fact_id: str):
    """Locate an ix:nonFraction element by its id. Returns None if absent/self-closing."""
    m = re.search(
        rb'<ix:nonFraction\b[^>]*?\sid="' + re.escape(fact_id.encode()) + rb'"[^>]*>', html
    )
    if not m or m.group(0).endswith(b"/>"):
        return None
    close = html.find(b"</ix:nonFraction>", m.end())
    if close == -1:
        return None
    return {"open_tag": m.group(0), "start": m.end(), "end": close, "inner": html[m.end():close]}


def parse_display(element: dict):
    """
    Read the number as printed in the filing. Only plain digit text in the
    ixt:num-dot-decimal format is rewritable ("—", spelled-out words and
    nested markup are not). Returns None if this element can't be edited.
    """
    open_tag = element["open_tag"]
    if _attr(open_tag, b"format") != NUM_FORMAT:
        return None
    text = element["inner"].decode("utf-8").strip()
    if not PLAIN_NUMBER.match(text):
        return None

    digits = text.replace(",", "")
    shown_decimals = len(digits.split(".")[1]) if "." in digits else 0
    scale = int((_attr(open_tag, b"scale") or b"0").decode())
    negative = _attr(open_tag, b"sign") == b"-"

    displayed = Decimal(digits)
    value = displayed * Decimal(10) ** scale * (-1 if negative else 1)
    return {
        "text": text,
        "scale": scale,
        "negative": negative,
        "shown_decimals": shown_decimals,
        "grouped": "," in text,
        "value": value,
        "step": Decimal(10) ** (scale - shown_decimals),
    }


def format_display(abs_displayed: Decimal, parsed: dict) -> str:
    quantum = Decimal(1).scaleb(-parsed["shown_decimals"])
    q = abs_displayed.quantize(quantum)
    if parsed["grouped"] or q >= 1000:
        return f"{q:,.{parsed['shown_decimals']}f}"
    return f"{q:.{parsed['shown_decimals']}f}"


def _coefficient(constraint: dict, fact_id: str) -> Decimal:
    if constraint["total_fact_id"] == fact_id:
        return Decimal(1)
    idx = constraint["component_fact_ids"].index(fact_id)
    return abs(Decimal(str(constraint["weights"][idx])))


def rank_candidates(unique_numeric: list, checked: list) -> list:
    """Facts used by >= 2 complete filer-calc constraints, most-connected first."""
    by_fact = defaultdict(list)
    for c in checked:
        if c["rule_source"] in solver.ENCODED_SOURCES and not c.get("partial"):
            for fid in set(c["involved_fact_ids"]):
                by_fact[fid].append(c)

    candidates = [
        (len(by_fact[f["fact_id"]]), f, by_fact[f["fact_id"]])
        for f in unique_numeric
        if f["value"] is not None and len(by_fact.get(f["fact_id"], [])) >= 2
    ]
    candidates.sort(key=lambda t: (-t[0], t[1]["fact_id"]))
    return candidates


def plan_injection(fact: dict, constraints_for_fact: list, html: bytes):
    """
    Decide the new value for every occurrence of a fact. Returns None if any
    occurrence can't be edited or disagrees with Arelle's value (a wrong
    parse or a real duplicate conflict — not a safe injection target).
    """
    occurrences = []
    for fid in fact["occurrences"]:
        element = find_element(html, fid)
        parsed = parse_display(element) if element else None
        if parsed is None or parsed["value"] != fact["value"]:
            return None
        occurrences.append({"fact_id": fid, "element": element, "parsed": parsed})

    step = max(o["parsed"]["step"] for o in occurrences)
    threshold = min(
        (c["band"] + abs(c["residual"])) / _coefficient(c, fact["fact_id"])
        for c in constraints_for_fact
    )
    delta = (threshold // step + 1) * step
    new_abs = abs(fact["value"]) + delta
    new_value = new_abs if fact["value"] >= 0 else -new_abs

    for o in occurrences:
        shown = new_abs / Decimal(10) ** o["parsed"]["scale"]
        o["new_text"] = format_display(shown, o["parsed"])
    return {"occurrences": occurrences, "delta": delta, "new_value": new_value}


def splice(html: bytes, occurrences: list) -> bytes:
    """Replace only the inner text of each edited element; every other byte is untouched."""
    out = html
    for o in sorted(occurrences, key=lambda o: o["element"]["start"], reverse=True):
        el = o["element"]
        out = out[:el["start"]] + o["new_text"].encode("utf-8") + out[el["end"]:]
    return out


def download_support_files(html: bytes, filing_url: str, dest: Path) -> None:
    """Fetch the filing's own schema + linkbases so Arelle can open a local copy."""
    base = filing_url.rsplit("/", 1)[0] + "/"
    dest.mkdir(parents=True, exist_ok=True)

    m = re.search(rb'<link:schemaRef\b[^>]*?xlink:href="([^"]+\.xsd)"', html)
    if not m:
        raise RuntimeError("no schemaRef found in filing")
    schema_name = m.group(1).decode()
    schema = fetch._get(base + schema_name).content
    (dest / schema_name).write_bytes(schema)

    for ref in re.findall(rb'<link:linkbaseRef\b[^>]*?xlink:href="([^"]+)"', schema):
        name = ref.decode()
        if name.startswith("http"):
            continue
        (dest / name).write_bytes(fetch._get(base + name).content)


def build_model_inputs(location: str):
    """Extraction + filer-calc and ratio-identity constraints + checks (no slow DQC pass) for a URL or local path."""
    model = extract.load_filing(location)
    records = extract.extract_facts(model)
    unique_numeric, _ = extract.dedupe_numeric_facts(records)
    templates = constraints_mod.build_filer_calc_templates(model)
    calc, _ = constraints_mod.instantiate_filer_calc_constraints(templates, unique_numeric)
    identities = ratios.identity_constraints(unique_numeric)
    return unique_numeric, check.check_constraints(calc + identities, unique_numeric)


def verify_bytes(content: bytes, support_dir: Path) -> dict:
    """Re-open a filing's bytes with Arelle (in a temp folder next to the support files) and run Z3."""
    work = Path(tempfile.mkdtemp(prefix="inject_verify_"))
    for f in support_dir.iterdir():
        shutil.copy(f, work / f.name)
    path = work / "filing.htm"
    path.write_bytes(content)
    unique, checked = build_model_inputs(str(path))
    result = solver.check_satisfiability(unique, checked)
    shutil.rmtree(work, ignore_errors=True)
    failing = [c["constraint_id"] for c in checked
               if c["rule_source"] in solver.ENCODED_SOURCES and not c.get("partial")
               and not c["pass_rounding_tolerance"]]
    off_exact = [
        {"constraint_id": c["constraint_id"], "residual": c["residual"], "band": c["band"],
         "counts_as_violation": not c["pass_rounding_tolerance"],
         "equation": _describe_violations([c["constraint_id"]], checked, {f["fact_id"]: f for f in unique})[0]}
        for c in checked
        if c["rule_source"] in solver.ENCODED_SOURCES and not c.get("partial") and not c["pass_zero_tolerance"]
    ]
    return {
        "status": result["status"],
        "unsat_core": result["unsat_core"],
        "check_py_failing_constraints": failing,
        "off_by_any_amount": off_exact,
    }


def _describe_violations(core: list, checked: list, fact_by_id: dict) -> list:
    out = []
    for c in checked:
        if c["constraint_id"] in core:
            tag = "" if c["rule_source"] == "filer-calc" else f"[{c['rule_source']}: {c['ratio_rule_id']}] "
            out.append(
                f"{tag}{fact_by_id[c['total_fact_id']]['concept']} should equal "
                + " + ".join(
                    f"{w:+g} x {fact_by_id[fid]['concept']}"
                    for w, fid in zip(c["weights"], c["component_fact_ids"])
                )
            )
    return out


def _json_default(o):
    return float(o) if isinstance(o, Decimal) else str(o)


def _period_label(period: dict) -> str:
    if period["type"] == "instant":
        return f"as of {period['instant'][:10]}"
    if period["type"] == "duration":
        return f"fiscal year {period['start'][:10]} to {period['end'][:10]}"
    return "period: forever"


def inject(filing_url: str, count: int = 1, rank: int = 0, label: str = "filing") -> dict:
    """
    Make `count` corrupted copies of one clean filing, each with ONE different
    fact changed (in every place that fact is printed). Layout:

      datasets/<LABEL>/<accession>/
        good.htm                clean original
        _support/               schema + linkbases Arelle needs
        for_llm/case_NN.htm     files to hand to the model under test
        answers/case_NN.txt     human answer key   (keep away from the model)
        answers/case_NN.json    same, machine-readable
        summary.json            one entry per case + verification status
    """
    accession = filing_url.rstrip("/").split("/")[-2]
    root = DATASETS_DIR / label / accession
    for sub in ("for_llm", "answers"):
        shutil.rmtree(root / sub, ignore_errors=True)
        (root / sub).mkdir(parents=True, exist_ok=True)
    support_dir = root / "_support"

    print("1/5 downloading original filing + schema files ...")
    good_html = fetch._get(filing_url).content
    (root / "good.htm").write_bytes(good_html)
    download_support_files(good_html, filing_url, support_dir)

    print("2/5 extracting + building constraints ...")
    unique_numeric, checked = build_model_inputs(filing_url)
    fact_by_id = {f["fact_id"]: f for f in unique_numeric}

    print("3/5 satisfiability gate on the clean filing ...")
    clean = solver.check_satisfiability(unique_numeric, checked)
    if clean["status"] != "sat":
        print("    UNSAT — filing discarded. core:", clean["unsat_core"])
        return {"discarded": True, "reason": "clean filing is UNSAT", "unsat_core": clean["unsat_core"]}
    print(f"    SAT ({clean['constraints_encoded']} constraints, {clean['variables']} variables)")
    good_check = verify_bytes(good_html, support_dir)
    print("    good.htm re-parsed from disk ->", good_check["status"])

    print(f"4/5 choosing up to {count} different facts and predicting each break ...")
    cases = []
    seen_cores = set()
    skipped = 0
    for connectivity, fact, cs in rank_candidates(unique_numeric, checked)[rank:]:
        if len(cases) >= count:
            break
        plan = plan_injection(fact, cs, good_html)
        if plan is None:
            skipped += 1
            continue
        corrupted = copy.deepcopy(unique_numeric)
        for f in corrupted:
            if f["fact_id"] == fact["fact_id"]:
                f["value"] = plan["new_value"]
        predicted = solver.check_satisfiability(corrupted, checked)
        core_key = tuple(predicted["unsat_core"])
        if predicted["status"] != "unsat" or core_key in seen_cores:
            skipped += 1
            continue
        seen_cores.add(core_key)
        cases.append((connectivity, fact, cs, plan, predicted))
        print(f"    case {len(cases):02d}: {fact['label']} {fact['value']} -> {plan['new_value']} "
              f"| Z3 core {predicted['unsat_core']}")
    print(f"    found {len(cases)} of {count} requested ({skipped} candidates skipped: not editable, "
          f"no UNSAT, or same broken rule as an earlier case)")

    print("5/5 writing each case and verifying it with Arelle + Z3 ...")
    summary = []
    for i, (connectivity, fact, cs, plan, predicted) in enumerate(cases, start=1):
        name = f"case_{i:02d}"
        bad_html = splice(good_html, plan["occurrences"])
        (root / "for_llm" / f"{name}.htm").write_bytes(bad_html)

        verification = verify_bytes(bad_html, support_dir)
        verified = (
            good_check["status"] == "sat"
            and verification["status"] == "unsat"
            and verification["unsat_core"] == predicted["unsat_core"]
        )
        all_broken = verification["check_py_failing_constraints"]
        details = _describe_violations(all_broken, checked, fact_by_id)

        truth = {
            "case": name,
            "test_file": f"for_llm/{name}.htm",
            "filing_url": filing_url,
            "accession": accession,
            "injected_fact": {
                "concept": fact["concept"],
                "label": fact["label"],
                "unique_fact_id": fact["fact_id"],
                "context": fact["context_ref"],
                "fiscal_period_label": _period_label(fact["context_ref"]["period"]),
                "unit": fact["unit"],
                "sections": fact["sections"],
                "old_value": fact["value"],
                "new_value": plan["new_value"],
                "delta": plan["delta"],
                "places_changed": len(plan["occurrences"]),
                "occurrences": [
                    {
                        "fact_id": o["fact_id"],
                        "old_text": o["parsed"]["text"],
                        "new_text": o["new_text"],
                        "scale": o["parsed"]["scale"],
                        "negative": o["parsed"]["negative"],
                    }
                    for o in plan["occurrences"]
                ],
            },
            "violated_constraints": all_broken,
            "z3_unsat_core": predicted["unsat_core"],
            "violated_constraint_details": details,
            "constraints_using_fact": [c["constraint_id"] for c in cs],
            "verification": verification,
            "verified": verified,
        }
        with open(root / "answers" / f"{name}.json", "w") as f:
            json.dump(truth, f, indent=2, default=_json_default)

        key = [
            "ANSWER KEY (do not show this to the model under test)",
            f"document to test : for_llm/{name}.htm",
            f"changed number   : {fact['label']} ({fact['concept']})",
            f"fiscal period    : {_period_label(fact['context_ref']['period'])}  "
            f"(contextRef=\"{fact['context_ref']['context_id']}\", unit={fact['unit']})",
            f"original value   : {fact['value']}",
            f"injected value   : {plan['new_value']}",
            f"places changed  : {len(plan['occurrences'])} in this one file (same number, printed {len(plan['occurrences'])}x)",
            "as printed       : " + ", ".join(
                f"{o['parsed']['text']} -> {o['new_text']} (tag id {o['fact_id']})" for o in plan["occurrences"]
            ),
            "sections         : " + "; ".join(fact["sections"]),
            "to find it by hand: open for_llm/" + name + ".htm in a text editor and search (Ctrl/Cmd-F) for one "
            "of these ix:nonFraction tag ids: " + ", ".join(f'id="{o["fact_id"]}"' for o in plan["occurrences"])
            + f'. Each shows "{plan["occurrences"][0]["new_text"]}" where the real 10-K (see good.htm, same tag id) '
            f'shows "{plan["occurrences"][0]["parsed"]["text"]}". The tag\'s contextRef attribute will read '
            f'"{fact["context_ref"]["context_id"]}", matching the fiscal period above.',
            f"broken rules    : {len(details)} (all fail the rounding band)",
            *[f"  - {d}" for d in details],
            f"rules off by any amount (exact test): {len(verification['off_by_any_amount'])}",
            *[
                f"  - gap {Decimal(str(r['residual'])):,.0f} vs allowed rounding {Decimal(str(r['band'])):,.0f} -> "
                + ("VIOLATION" if r["counts_as_violation"] else "within rounding, not a violation")
                + f" | {r['equation']}"
                for r in verification["off_by_any_amount"]
            ],
            "clean original   : good.htm",
        ]
        (root / "answers" / f"{name}.txt").write_text("\n".join(key) + "\n")

        print(f"    {name}: verified={verified}  ({fact['label']})")
        summary.append({"case": name, "changed": fact["label"], "concept": fact["concept"],
                        "old_value": fact["value"], "new_value": plan["new_value"],
                        "broken_rules": all_broken, "verified": verified})

    with open(root / "summary.json", "w") as f:
        json.dump({"filing_url": filing_url, "good_status": good_check["status"],
                   "requested": count, "produced": len(cases), "cases": summary},
                  f, indent=2, default=_json_default)

    print(f"\n{len(cases)} case(s), {sum(s['verified'] for s in summary)} verified.")
    print(f"  give the LLM files from : {root / 'for_llm'}")
    print(f"  answer keys (keep away) : {root / 'answers'}")
    return {"cases": summary}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="AAPL", help="ticker or filing URL")
    ap.add_argument("--count", type=int, default=1, help="how many different errors (one file each)")
    ap.add_argument("--rank", type=int, default=0, help="skip this many top candidates first")
    a = ap.parse_args()

    if a.target.startswith("http"):
        url, label = a.target, "filing"
    else:
        cik = fetch.get_cik_for_ticker(a.target)
        latest = fetch.get_recent_10k_filings(cik, limit=1)[0]
        url = fetch.build_filing_url(cik, latest["accession_number"], latest["primary_document"])
        label = a.target.upper()
    inject(url, count=a.count, rank=a.rank, label=label)
