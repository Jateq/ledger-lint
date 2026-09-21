"""
report.py — Step 6 of the pipeline: one summary per filing.

Pulls together extraction counts, constraint coverage, consistency rates,
orphan counts, and any extraction/rule-execution failures into a single
report dict for one filing. Filings are never pooled — each one gets its
own report, since real variation between companies/years is expected.
"""

import csv
import json
from decimal import Decimal
from pathlib import Path


def _ratio_sections(ratio_out, checked_constraints):
    """Ratio-library usage + identity results (kept out of the filer-calc headline)."""
    if ratio_out is None:
        return None
    ident = [c for c in checked_constraints if c["rule_source"] == "ratio-identity"]
    complete = [c for c in ident if not c.get("partial")]
    by_rule = {}
    for c in complete:
        row = by_rule.setdefault(c["ratio_rule_id"], {"checked": 0, "failed": 0})
        row["checked"] += 1
        row["failed"] += 0 if c["pass_rounding_tolerance"] else 1
    return {
        "fiscal_period": ratio_out["fiscal_period"],
        "usage": ratio_out["summary"],
        "ratios": [
            {"id": r["id"], "name": r["name"], "category": r["category"], "status": r["status"],
             "reason": r["reason"], "detail": r["detail"], "value": r["value"],
             "sources": r["sources"],
             "inputs": [{"term": i["term"], "at": i["at"], "concept": i.get("concept"),
                         "fact_id": i.get("fact_id"), "assumed_zero": i.get("assumed_zero", False)}
                        for i in r["inputs"]],
             "advisory": r["advisory"], "crosscheck": r.get("crosscheck")}
            for r in ratio_out["results"]
        ],
        "identities": {
            "checked": len(complete),
            "failed_band": sum(1 for c in complete if not c["pass_rounding_tolerance"]),
            "by_rule": by_rule,
            "note": "identities like assets = liabilities + equity are rules a clean filing must satisfy; "
                    "ratios themselves are never treated as errors.",
        },
    }


def build_report(url: str, extraction_counts: dict, checked_constraints: list,
                  failures: list, marked_facts: list, orphan_stats: dict,
                  ratio_out: dict | None = None, z3: dict | None = None,
                  injection_dataset: dict | None = None) -> dict:
    calc_all = [c for c in checked_constraints if c["rule_source"] == "filer-calc"]
    dqc = [c for c in checked_constraints if c["rule_source"] == "DQC"]

    # Headline consistency counts only complete constraints (every declared
    # child reported). Partial ones can be artifacts — e.g. a total reported
    # with no dimensions whose children only exist under a dimension — so
    # they are reported separately, not mixed into the pass rate.
    calc = [c for c in calc_all if not c.get("partial")]
    partial = [c for c in calc_all if c.get("partial")]

    calc_pass_zero = sum(1 for c in calc if c["pass_zero_tolerance"])
    calc_pass_rounding = sum(1 for c in calc if c["pass_rounding_tolerance"])
    calc_fail = len(calc) - calc_pass_rounding

    spans_count = sum(1 for c in checked_constraints if c.get("spans_sections"))

    return {
        "filing_url": url,
        "fact_counts": {
            "total_fact_occurrences": extraction_counts["total_fact_occurrences"],
            "numeric_fact_occurrences": extraction_counts["numeric_fact_occurrences"],
            "nonnumeric_fact_occurrences": (
                extraction_counts["total_fact_occurrences"] - extraction_counts["numeric_fact_occurrences"]
            ),
            "unique_numeric_facts": extraction_counts["unique_numeric_facts"],
            "conflicting_unique_facts": extraction_counts.get("conflicting_unique_facts", 0),
        },
        "coverage": {
            "unique_numeric_facts": orphan_stats["unique_numeric_facts"],
            "covered_facts": orphan_stats["covered_facts"],
            "orphan_facts": orphan_stats["orphan_facts"],
            "coverage_pct": round(orphan_stats["coverage_pct"], 2),
            "by_rule_source": {
                src: {"covered_facts": v["covered_facts"], "coverage_pct": round(v["coverage_pct"], 2)}
                for src, v in orphan_stats.get("by_rule_source", {}).items()
            },
        },
        "constraints": {
            "total": len(checked_constraints),
            "by_rule_source": {
                "filer-calc": len(calc_all),
                "DQC": len(dqc),
                "ratio-identity": sum(1 for c in checked_constraints if c["rule_source"] == "ratio-identity"),
            },
            "spans_sections_count": spans_count,
            "spans_sections_pct": (
                round(spans_count / len(checked_constraints) * 100, 2) if checked_constraints else None
            ),
        },
        "consistency": {
            "filer_calc": {
                "checked": len(calc),
                "pass_exact_zero_tolerance": calc_pass_zero,
                "pass_only_within_rounding_band": calc_pass_rounding - calc_pass_zero,
                "fail_even_with_rounding_band": calc_fail,
                "pass_rate_pct": round(calc_pass_rounding / len(calc) * 100, 2) if calc else None,
                "partial_checked": len(partial),
                "partial_failed": sum(1 for c in partial if not c["pass_rounding_tolerance"]),
            },
            "dqc": {
                "violations_found": len(dqc),
                "note": (
                    "DQC only logs violations; a rule with nothing wrong never appears in "
                    "the log, so there is no observable 'passed' count to compute a rate from."
                ),
            },
        },
        "failures": failures,
        "ratio_library": _ratio_sections(ratio_out, checked_constraints),
        "z3": z3,
        "injection_dataset": injection_dataset,
    }


def _json_default(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"not JSON serializable: {type(obj)}")


def report_to_json(report: dict) -> str:
    return json.dumps(report, indent=2, default=_json_default)


def summary_row(report: dict) -> dict:
    """Flatten one filing's report into a single CSV row of headline numbers."""
    fc = report["fact_counts"]
    cov = report["coverage"]
    con = report["constraints"]
    cal = report["consistency"]["filer_calc"]
    return {
        "filing_url": report["filing_url"],
        "total_fact_occurrences": fc["total_fact_occurrences"],
        "numeric_fact_occurrences": fc["numeric_fact_occurrences"],
        "nonnumeric_fact_occurrences": fc["nonnumeric_fact_occurrences"],
        "unique_numeric_facts": fc["unique_numeric_facts"],
        "conflicting_unique_facts": fc["conflicting_unique_facts"],
        "covered_facts": cov["covered_facts"],
        "orphan_facts": cov["orphan_facts"],
        "coverage_pct": cov["coverage_pct"],
        "coverage_pct_filer_calc": cov["by_rule_source"].get("filer-calc", {}).get("coverage_pct", 0.0),
        "coverage_pct_dqc": cov["by_rule_source"].get("DQC", {}).get("coverage_pct", 0.0),
        "constraints_total": con["total"],
        "constraints_filer_calc": con["by_rule_source"]["filer-calc"],
        "constraints_dqc": con["by_rule_source"]["DQC"],
        "spans_sections_count": con["spans_sections_count"],
        "spans_sections_pct": con["spans_sections_pct"],
        "calc_checked": cal["checked"],
        "calc_pass_exact": cal["pass_exact_zero_tolerance"],
        "calc_pass_within_band_only": cal["pass_only_within_rounding_band"],
        "calc_fail_even_with_band": cal["fail_even_with_rounding_band"],
        "calc_pass_rate_pct": cal["pass_rate_pct"],
        "calc_partial_checked": cal["partial_checked"],
        "calc_partial_failed": cal["partial_failed"],
        "dqc_violations": report["consistency"]["dqc"]["violations_found"],
        "failures": len(report["failures"]),
        "constraints_ratio_identity": con["by_rule_source"].get("ratio-identity", 0),
        "ratios_computed": (report.get("ratio_library") or {}).get("usage", {}).get("computed", ""),
        "ratios_not_usable": (report.get("ratio_library") or {}).get("usage", {}).get("not_usable", ""),
        "ratio_identities_checked": (report.get("ratio_library") or {}).get("identities", {}).get("checked", ""),
        "ratio_identities_failed": (report.get("ratio_library") or {}).get("identities", {}).get("failed_band", ""),
        "z3_status": (report.get("z3") or {}).get("status", ""),
    }


def across_filings(reports: list) -> dict:
    """
    How many of the tested filings had every checked filer-calc constraint
    pass (under the rounding band). Per-filing reports remain the primary
    result; this is only a tally, not a pooled average.
    """
    checked = [r for r in reports if r["consistency"]["filer_calc"]["checked"] > 0]
    consistent = [
        r for r in checked
        if r["consistency"]["filer_calc"]["fail_even_with_rounding_band"] == 0
    ]
    usage = {}
    with_ratios = [r for r in reports if r.get("ratio_library")]
    for r in with_ratios:
        for x in r["ratio_library"]["ratios"]:
            row = usage.setdefault(x["id"], {"computed": 0, "not_usable": 0})
            row["computed" if x["status"] == "computed" else "not_usable"] += 1
    return {
        "filings_tested": len(reports),
        "filings_with_filer_calc_checks": len(checked),
        "filings_fully_consistent": len(consistent),
        "fraction_consistent": (len(consistent) / len(checked)) if checked else None,
        "ratio_usage_across_filings": usage,
    }


def write_ratios_csv(ratio_rows: list, path) -> None:
    """One row per ratio for one filing: value or why it was not usable, with sources and inputs."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(ratio_rows[0].keys()))
        writer.writeheader()
        writer.writerows(ratio_rows)


def write_summary_csv(reports: list, path) -> None:
    """One row per filing. Rows stay separate — nothing is pooled or averaged."""
    rows = [summary_row(r) for r in reports]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


CONSTRAINT_CSV_FIELDS = [
    "constraint_id", "rule_source", "kind", "rule_code", "spans_sections", "result",
    "total_value", "sum_of_components", "residual", "band",
    "pass_zero_tolerance", "pass_rounding_tolerance", "headroom",
    "min_injectable_delta", "involved_fact_ids", "message", "ratio_rule_id",
]


def write_constraints_csv(checked_constraints: list, path) -> None:
    """One row per constraint, with the per-constraint numbers the JSON summary leaves out."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CONSTRAINT_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for c in checked_constraints:
            row = dict(c)
            row["involved_fact_ids"] = ";".join(c.get("involved_fact_ids", []))
            writer.writerow(row)


def print_report(report: dict) -> None:
    print(f"Filing: {report['filing_url']}")

    fc = report["fact_counts"]
    print("\nFact counts:")
    print(f"  total fact occurrences:      {fc['total_fact_occurrences']}")
    print(f"  numeric fact occurrences:    {fc['numeric_fact_occurrences']}")
    print(f"  nonnumeric fact occurrences: {fc['nonnumeric_fact_occurrences']}")
    print(f"  unique numeric facts:        {fc['unique_numeric_facts']}")
    print(f"  merged facts with conflicting values: {fc['conflicting_unique_facts']}")

    cov = report["coverage"]
    print("\nCoverage:")
    print(f"  covered facts: {cov['covered_facts']} / {cov['unique_numeric_facts']} ({cov['coverage_pct']}%)")
    print(f"  orphan facts:  {cov['orphan_facts']}")
    for src, v in cov["by_rule_source"].items():
        print(f"    covered by {src}: {v['covered_facts']} ({v['coverage_pct']}%)")

    con = report["constraints"]
    print("\nConstraints:")
    print(f"  total: {con['total']}  (filer-calc: {con['by_rule_source']['filer-calc']}, DQC: {con['by_rule_source']['DQC']}, "
          f"ratio-identity: {con['by_rule_source'].get('ratio-identity', 0)})")
    print(f"  spanning sections: {con['spans_sections_count']} ({con['spans_sections_pct']}%)")

    cal = report["consistency"]["filer_calc"]
    print("\nConsistency (filer-calc):")
    print(f"  checked: {cal['checked']}")
    print(f"  pass exactly (zero tolerance):        {cal['pass_exact_zero_tolerance']}")
    print(f"  pass only within rounding band:       {cal['pass_only_within_rounding_band']}")
    print(f"  fail even with rounding band:         {cal['fail_even_with_rounding_band']}")
    print(f"  pass rate: {cal['pass_rate_pct']}%")
    print(f"  (complete constraints only — every declared child reported)")
    print(f"  partial, reported separately (some children unreported): {cal['partial_checked']}, failing: {cal['partial_failed']}")

    dqc = report["consistency"]["dqc"]
    print(f"\nDQC violations found: {dqc['violations_found']}")

    rl = report.get("ratio_library")
    if rl:
        u = rl["usage"]
        print(f"\nRatio library ({rl['fiscal_period']['start'][:10]} .. {rl['fiscal_period']['end'][:10]}):")
        print(f"  ratios in library: {u['ratios_in_library']}   computed: {u['computed']}   not usable: {u['not_usable']}")
        print(f"  not usable by reason: {u['not_usable_by_reason']}")
        print(f"  computed ratios by source PDF: {u['computed_by_source_pdf']}")
        print(f"  outside a textbook rule of thumb (advisory only): {u['outside_rule_of_thumb_advisory']}")
        print(f"  EPS cross-check (computed vs reported): {u['eps_crosscheck']}")
        idn = rl["identities"]
        print(f"  ratio identities: {idn['checked']} checked, {idn['failed_band']} fail the rounding band")

    z3 = report.get("z3")
    if z3:
        print(f"\nZ3 satisfiability: {z3['status'].upper()} ({z3['constraints_encoded']} constraints, {z3['variables']} variables)")
        if z3["unsat_core"]:
            print("  broken:", z3["unsat_core"][:5])

    inj = report.get("injection_dataset")
    if inj:
        print(f"\nInjection dataset on disk: {inj['produced']} case(s), "
              f"{sum(1 for c in inj['cases'] if c['verified'])} verified")

    fails = report["failures"]
    print(f"\nExtraction/rule-execution failures: {len(fails)}")
    for f in fails[:5]:
        print(" -", f)


if __name__ == "__main__":
    import sys

    import extract
    import constraints as constraints_mod
    import check
    import orphans

    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
    )

    print(f"Loading {url} ...")
    model = extract.load_filing(url)
    records = extract.extract_facts(model)
    unique_numeric, extraction_counts = extract.dedupe_numeric_facts(records)

    print("Building + checking constraints ...")
    build_result = constraints_mod.build_constraints(model, url, records, unique_numeric)
    checked = check.check_constraints(build_result["constraints"], unique_numeric)

    print("Marking orphans ...")
    marked = orphans.mark_orphans(unique_numeric, checked)
    orphan_stats = orphans.orphan_summary(marked, checked)

    report = build_report(url, extraction_counts, checked, build_result["failures"], marked, orphan_stats)

    print("\n" + "=" * 60)
    print_report(report)

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    out_path = sys.argv[2] if len(sys.argv) > 2 else str(reports_dir / "report.json")
    with open(out_path, "w") as f:
        f.write(report_to_json(report))
    print(f"\nFull report written to {out_path}")
