"""
check.py — Step 4 of the pipeline: is each constraint actually satisfied?

DQC constraints already carry their verdict (a DQC finding IS a failure —
see constraints.py) so this file passes those through untouched. The real
work here is filer-calc constraints: constraints.py only said "these facts
should sum to this total," it never did the arithmetic. This file does it,
under two tolerance modes:

  zero tolerance     — residual must be exactly 0
  rounding tolerance — residual must fall within the propagated rounding
                        band implied by every involved fact's `decimals`

Also computes, per Asmaa's request, the smallest change to one fact that
would push a passing constraint outside its rounding band — a measure of
how fragile each check actually is.
"""

from decimal import Decimal


SUM_SOURCES = ("filer-calc", "ratio-identity")


def fact_tolerance(decimals) -> Decimal:
    """
    Half of the smallest increment a fact's `decimals` attribute could
    round away — e.g. decimals=-6 (rounded to the nearest million) implies
    the true value could be off by up to 500,000 in either direction.
    decimals of None/"INF" means the value is exact (no rounding at all).
    """
    if decimals is None or decimals == "INF":
        return Decimal(0)
    return Decimal(10) ** Decimal(-int(decimals)) * Decimal("0.5")


def check_filer_calc_constraint(constraint: dict, fact_by_id: dict) -> dict:
    """
    Compute residual, propagated rounding band, pass/fail under both
    tolerance modes, and the smallest injectable delta, for one
    filer-calc (sum) constraint.
    """
    total_fact = fact_by_id[constraint["total_fact_id"]]
    component_facts = [fact_by_id[fid] for fid in constraint["component_fact_ids"]]
    weights = constraint["weights"]

    total_value = total_fact["value"]
    sum_of_components = sum(
        (Decimal(str(w)) * f["value"] for w, f in zip(weights, component_facts)), Decimal(0)
    )
    residual = sum_of_components - total_value

    band = fact_tolerance(total_fact["decimals"])
    for w, f in zip(weights, component_facts):
        band += abs(Decimal(str(w))) * fact_tolerance(f["decimals"])

    pass_zero_tolerance = residual == 0
    pass_rounding_tolerance = abs(residual) <= band
    headroom = band - abs(residual)

    max_abs_weight = max([Decimal(1)] + [abs(Decimal(str(w))) for w in weights])
    min_injectable_delta = headroom / max_abs_weight if headroom >= 0 else Decimal(0)

    return {
        **constraint,
        "total_value": total_value,
        "sum_of_components": sum_of_components,
        "residual": residual,
        "band": band,
        "pass_zero_tolerance": pass_zero_tolerance,
        "pass_rounding_tolerance": pass_rounding_tolerance,
        "headroom": headroom,
        "min_injectable_delta": min_injectable_delta,
        "result": "PASS" if pass_rounding_tolerance else "FAIL",
    }


def check_constraints(constraints: list, unique_numeric_facts: list) -> list:
    """
    Check every constraint. Filer-calc constraints get real arithmetic
    checks; DQC constraints (already resolved — a finding IS a failure)
    pass through unchanged so downstream code can treat both sources
    uniformly (every constraint ends up with a "result" field).
    """
    fact_by_id = {f["fact_id"]: f for f in unique_numeric_facts}

    checked = []
    for constraint in constraints:
        if constraint["rule_source"] in SUM_SOURCES:
            checked.append(check_filer_calc_constraint(constraint, fact_by_id))
        else:
            checked.append(constraint)

    return checked


if __name__ == "__main__":
    import sys

    import extract
    import constraints as constraints_mod

    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
    )

    print(f"Loading {url} ...")
    model = extract.load_filing(url)
    records = extract.extract_facts(model)
    unique_numeric, counts = extract.dedupe_numeric_facts(records)
    print("extraction counts:", counts)

    print("\nBuilding constraints ...")
    build_result = constraints_mod.build_constraints(model, url, records, unique_numeric)
    all_constraints = build_result["constraints"]
    print(f"total constraints: {len(all_constraints)}")
    print(f"failures during constraint-building: {len(build_result['failures'])}")

    print("\nChecking constraints ...")
    checked = check_constraints(all_constraints, unique_numeric)

    calc_checked = [c for c in checked if c["rule_source"] == "filer-calc"]
    passed_zero = sum(1 for c in calc_checked if c["pass_zero_tolerance"])
    passed_rounding = sum(1 for c in calc_checked if c["pass_rounding_tolerance"])
    print(f"filer-calc constraints: {len(calc_checked)}")
    print(f"  pass under zero tolerance:     {passed_zero}/{len(calc_checked)}")
    print(f"  pass under rounding tolerance: {passed_rounding}/{len(calc_checked)}")

    failing = [c for c in calc_checked if not c["pass_rounding_tolerance"]]
    if failing:
        print("\n--- failing filer-calc constraints ---")
        for c in failing[:5]:
            print(c["constraint_id"], "residual:", c["residual"], "band:", c["band"])
    else:
        print("\nno failing filer-calc constraints")

    tightest = sorted(calc_checked, key=lambda c: c["headroom"])[:3]
    print("\n--- tightest headroom (most fragile) ---")
    for c in tightest:
        print(c["constraint_id"], "residual:", c["residual"], "band:", c["band"],
              "headroom:", c["headroom"], "min_injectable_delta:", c["min_injectable_delta"])
