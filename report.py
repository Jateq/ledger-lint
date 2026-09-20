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


def build_report(url: str, extraction_counts: dict, checked_constraints: list,
                  failures: list, marked_facts: list, orphan_stats: dict) -> dict:
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
    return {
        "filings_tested": len(reports),
        "filings_with_filer_calc_checks": len(checked),
        "filings_fully_consistent": len(consistent),
        "fraction_consistent": (len(consistent) / len(checked)) if checked else None,
    }


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
    "min_injectable_delta", "involved_fact_ids", "message",
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
    print(f"  total: {con['total']}  (filer-calc: {con['by_rule_source']['filer-calc']}, DQC: {con['by_rule_source']['DQC']})")
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
