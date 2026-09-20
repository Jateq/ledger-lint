"""
orphans.py — Step 5 of the pipeline: which facts does nothing check?

Whole-filing scope, computed after every constraint (filer-calc + DQC) has
already been built — a fact only counts as covered if it participates in
at least one constraint ANYWHERE in the filing, not just within one
section. Participation is what matters here, not whether the constraint
passed or failed — a fact inside a failing constraint is still "covered,"
just covered by a rule that didn't hold.
"""


def get_covered_fact_ids(constraints: list) -> set:
    """Every fact_id that shows up in at least one constraint, from any rule_source."""
    covered = set()
    for constraint in constraints:
        covered.update(constraint.get("involved_fact_ids", []))
    return covered


def mark_orphans(unique_numeric_facts: list, constraints: list) -> list:
    """
    Tag every unique numeric fact with is_orphan_in_filing: True if it
    never participates in any constraint, anywhere in the filing.
    """
    covered = get_covered_fact_ids(constraints)
    return [
        {**fact, "is_orphan_in_filing": fact["fact_id"] not in covered}
        for fact in unique_numeric_facts
    ]


def coverage_by_source(marked_facts: list, constraints: list) -> dict:
    """
    For each rule_source, how many unique numeric facts it covers on its
    own (a fact covered by two sources counts once under each).
    """
    total = len(marked_facts)
    numeric_ids = {f["fact_id"] for f in marked_facts}

    ids_by_source: dict = {}
    for constraint in constraints:
        ids_by_source.setdefault(constraint["rule_source"], set()).update(
            constraint.get("involved_fact_ids", [])
        )

    return {
        source: {
            "covered_facts": len(ids & numeric_ids),
            "coverage_pct": (len(ids & numeric_ids) / total * 100) if total else 0.0,
        }
        for source, ids in ids_by_source.items()
    }


def orphan_summary(marked_facts: list, constraints: list | None = None) -> dict:
    """Coverage stats for the report: how many unique numeric facts are covered vs. orphaned."""
    total = len(marked_facts)
    orphan_count = sum(1 for f in marked_facts if f["is_orphan_in_filing"])
    covered_count = total - orphan_count
    coverage_pct = (covered_count / total * 100) if total else 0.0

    return {
        "unique_numeric_facts": total,
        "covered_facts": covered_count,
        "orphan_facts": orphan_count,
        "coverage_pct": coverage_pct,
        "by_rule_source": coverage_by_source(marked_facts, constraints) if constraints else {},
    }


if __name__ == "__main__":
    import sys

    import extract
    import constraints as constraints_mod
    import check

    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
    )

    print(f"Loading {url} ...")
    model = extract.load_filing(url)
    records = extract.extract_facts(model)
    unique_numeric, counts = extract.dedupe_numeric_facts(records)
    print("extraction counts:", counts)

    print("\nBuilding + checking constraints ...")
    build_result = constraints_mod.build_constraints(model, url, records, unique_numeric)
    checked = check.check_constraints(build_result["constraints"], unique_numeric)
    print(f"total constraints: {len(checked)}")

    print("\nMarking orphans ...")
    marked = mark_orphans(unique_numeric, checked)
    summary = orphan_summary(marked)
    print(summary)

    orphans = [f for f in marked if f["is_orphan_in_filing"]]
    print(f"\n--- sample orphan facts ({min(5, len(orphans))} of {len(orphans)}) ---")
    for f in orphans[:5]:
        print(f["concept"], "|", f["label"], "|", f["value"], "|", f["sections"])
