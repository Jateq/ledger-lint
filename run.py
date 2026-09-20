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

from pathlib import Path

import fetch
import extract
import constraints as constraints_mod
import check
import orphans
import report as report_mod


def get_filing_urls(ticker: str, limit: int = 1) -> list:
    """Ticker -> list of filing document URLs, most recent first."""
    cik = fetch.get_cik_for_ticker(ticker)
    filings = fetch.get_recent_10k_filings(cik, limit=limit)
    return [
        fetch.build_filing_url(cik, f["accession_number"], f["primary_document"])
        for f in filings
    ]


def run_one_filing(url: str) -> dict:
    """
    Run the full pipeline for one filing: extract -> build constraints ->
    check -> find orphans -> build report. Returns the report dict.
    """
    print(f"Loading {url} ...")
    model = extract.load_filing(url)
    records = extract.extract_facts(model)
    unique_numeric, extraction_counts = extract.dedupe_numeric_facts(records)
    print(f"  extracted: {extraction_counts}")

    print("  building constraints (filer-calc + DQC — DQC takes a few minutes) ...")
    build_result = constraints_mod.build_constraints(model, url, records, unique_numeric)

    print("  checking constraints ...")
    checked = check.check_constraints(build_result["constraints"], unique_numeric)

    print("  marking orphans ...")
    marked = orphans.mark_orphans(unique_numeric, checked)
    orphan_stats = orphans.orphan_summary(marked, checked)

    report = report_mod.build_report(
        url, extraction_counts, checked, build_result["failures"], marked, orphan_stats
    )
    return report, checked


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
        report, checked = run_one_filing(url)
        all_reports.append(report)

        print("\n" + "=" * 60)
        report_mod.print_report(report)

        accession = url.rstrip("/").split("/")[-2]
        base = reports_dir / f"report_{ticker}_{accession}"
        with open(f"{base}.json", "w") as f:
            f.write(report_mod.report_to_json(report))
        report_mod.write_constraints_csv(checked, f"{base}_constraints.csv")
        print(f"\nWritten: {base}.json and {base}_constraints.csv")

    summary_path = reports_dir / f"summary_{ticker}.csv"
    report_mod.write_summary_csv(all_reports, summary_path)
    print(f"Written: {summary_path} (one row per filing)")

    tally = report_mod.across_filings(all_reports)
    print(
        f"Across tested filings: {tally['filings_fully_consistent']} of "
        f"{tally['filings_with_filer_calc_checks']} had every checked filer-calc constraint pass"
    )
