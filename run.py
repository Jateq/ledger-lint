"""
run.py — orchestrates the pipeline end-to-end for one company.

This is the file that actually connects every other file together: ticker
in, one full report out, per filing. No copying URLs or intermediate
results between files by hand.

Usage:
    python run.py AAPL
    python run.py AAPL --limit 3   # process the 3 most recent 10-Ks instead of 1

Each filing gets its own report under reports/ (report_<TICKER>_<accession>.json)
— filings are never pooled together, since real variation between
companies/years is expected and meaningful.
"""

import json
from pathlib import Path

import fetch
import extract
import constraints as constraints_mod
import check
import orphans
import ratios
import report as report_mod
import solver


def get_filing_urls(ticker: str, limit: int = 1) -> list:
    """Ticker -> list of filing document URLs, most recent first."""
    cik = fetch.get_cik_for_ticker(ticker)
    filings = fetch.get_recent_10k_filings(cik, limit=limit)
    return [
        fetch.build_filing_url(cik, f["accession_number"], f["primary_document"])
        for f in filings
    ]


def run_one_filing(url: str, ticker: str = "") -> tuple:
    """
    Run the full pipeline for one filing: extract -> build constraints (filer-calc,
    DQC, ratio identities) -> check -> Z3 -> ratio library -> orphans -> report.
    Returns (report, checked_constraints, ratio_rows).
    """
    print(f"Loading {url} ...")
    model = extract.load_filing(url)
    records = extract.extract_facts(model)
    unique_numeric, extraction_counts = extract.dedupe_numeric_facts(records)
    print(f"  extracted: {extraction_counts}")

    print("  building constraints (filer-calc + DQC — DQC takes a few minutes) ...")
    build_result = constraints_mod.build_constraints(model, url, records, unique_numeric)

    print("  ratio library (formulas from the reference PDFs) ...")
    ratio_analysis = ratios.analyze(unique_numeric)

    print("  checking constraints ...")
    checked = check.check_constraints(build_result["constraints"], unique_numeric) + ratio_analysis["identities"]

    print("  Z3 satisfiability (filer-calc + ratio identities) ...")
    z3_result = solver.check_satisfiability(unique_numeric, checked)

    print("  marking orphans ...")
    marked = orphans.mark_orphans(unique_numeric, checked)
    orphan_stats = orphans.orphan_summary(marked, checked)

    accession = url.rstrip("/").split("/")[-2]
    summary_path = Path("datasets") / ticker / accession / "summary.json"
    injection = json.loads(summary_path.read_text()) if ticker and summary_path.exists() else None

    report = report_mod.build_report(
        url, extraction_counts, checked, build_result["failures"], marked, orphan_stats,
        ratio_out=ratio_analysis["ratios"], z3=z3_result, injection_dataset=injection,
    )
    return report, checked, ratios.ratio_rows(ratio_analysis["ratios"])


if __name__ == "__main__":
    import sys

    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    limit = 1
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    print(f"Resolving {ticker} -> filing URLs ...")
    urls = get_filing_urls(ticker, limit=limit)
    print(f"Found {len(urls)} filing(s):")
    for url in urls:
        print(" -", url)

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)

    all_reports = []
    for url in urls:
        print()
        report, checked, ratio_rows = run_one_filing(url, ticker.upper())
        all_reports.append(report)

        print("\n" + "=" * 60)
        report_mod.print_report(report)

        accession = url.rstrip("/").split("/")[-2]
        base = reports_dir / f"report_{ticker}_{accession}"
        with open(f"{base}.json", "w") as f:
            f.write(report_mod.report_to_json(report))
        report_mod.write_constraints_csv(checked, f"{base}_constraints.csv")
        report_mod.write_ratios_csv(ratio_rows, f"{base}_ratios.csv")
        print(f"\nWritten: {base}.json, {base}_constraints.csv and {base}_ratios.csv")

    summary_path = reports_dir / f"summary_{ticker}.csv"
    report_mod.write_summary_csv(all_reports, summary_path)
    print(f"Written: {summary_path} (one row per filing)")

    tally = report_mod.across_filings(all_reports)
    print(
        f"Across tested filings: {tally['filings_fully_consistent']} of "
        f"{tally['filings_with_filer_calc_checks']} had every checked filer-calc constraint pass"
    )
    usage = tally["ratio_usage_across_filings"]
    if usage:
        n = len(all_reports)
        always = sum(1 for u in usage.values() if u["computed"] == n)
        never = sum(1 for u in usage.values() if u["computed"] == 0)
        print(f"Ratio usage across {n} filing(s): {len(usage)} ratios in library, "
              f"{always} computable in every filing, {never} in none")
