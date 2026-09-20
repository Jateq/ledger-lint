"""
solver.py — global satisfiability check of a filing's constraints with Z3.

check.py tests each constraint on its own. This file asks the stronger
question: can ALL the constraints hold at once, given the values the filing
actually reports? That yes/no is the gate for the fault-injection dataset:
only a filing that is provably SAT ("clean") is worth corrupting, because
after one controlled change the solver must flip to UNSAT and its unsat
core says exactly which constraints the change broke.

Model:
  - every unique numeric fact -> one Z3 Real variable, pinned to the value
    the filing reports (pins are hard facts, not part of the unsat core)
  - every complete filer-calc constraint -> two tracked inequalities,
        sum(weight_i * child_i) - total  <=  band
        sum(weight_i * child_i) - total  >= -band
    where band is the propagated rounding band from check.py
Partial constraints (some declared children unreported) are left out: they
can be artifacts of context matching and would make a clean filing look
UNSAT. DQC findings are tagging checks with no arithmetic, so they add no
equations here.
"""

from decimal import Decimal

import z3


def to_real(value: Decimal):
    """Exact rational Z3 constant from a Decimal (no float rounding)."""
    return z3.RealVal(format(Decimal(value), "f"))


def build_solver(unique_numeric_facts: list, checked_constraints: list):
    """
    Returns (solver, variables, tracked) where variables maps fact_id -> Z3
    Real and tracked maps each constraint_id to the Z3 label(s) asserted for
    it, so an unsat core can be translated back to constraint ids.
    """
    solver = z3.Solver()

    variables = {}
    for fact in unique_numeric_facts:
        if fact["value"] is None:
            continue
        var = z3.Real(f"v_{fact['fact_id']}")
        variables[fact["fact_id"]] = var
        solver.add(var == to_real(fact["value"]))

    tracked = {}
    for c in checked_constraints:
        if c["rule_source"] != "filer-calc" or c.get("partial"):
            continue

        total = variables[c["total_fact_id"]]
        parts = z3.Sum([
            to_real(Decimal(str(w))) * variables[fid]
            for w, fid in zip(c["weights"], c["component_fact_ids"])
        ])
        residual = parts - total
        band = to_real(c["band"])

        upper = z3.Bool(f"{c['constraint_id']}::upper")
        lower = z3.Bool(f"{c['constraint_id']}::lower")
        solver.assert_and_track(residual <= band, upper)
        solver.assert_and_track(residual >= -band, lower)
        tracked[c["constraint_id"]] = (upper, lower)

    return solver, variables, tracked


def check_satisfiability(unique_numeric_facts: list, checked_constraints: list) -> dict:
    """
    SAT  -> the filing is internally consistent under every complete
            filer-calc constraint at once (safe to use as a clean document).
    UNSAT-> "unsat_core" lists the constraint ids that cannot all hold.
    """
    solver, variables, tracked = build_solver(unique_numeric_facts, checked_constraints)
    result = solver.check()

    out = {
        "status": str(result),
        "variables": len(variables),
        "constraints_encoded": len(tracked),
        "unsat_core": [],
    }
    if result == z3.unsat:
        core_labels = {str(label) for label in solver.unsat_core()}
        out["unsat_core"] = sorted(cid for cid in tracked if f"{cid}::upper" in core_labels or f"{cid}::lower" in core_labels)
    return out


def _load_clean_inputs(url: str):
    """Extraction + filer-calc constraints + checks, without the slow DQC pass."""
    import check
    import constraints as constraints_mod
    import extract

    model = extract.load_filing(url)
    records = extract.extract_facts(model)
    unique_numeric, counts = extract.dedupe_numeric_facts(records)

    templates = constraints_mod.build_filer_calc_templates(model)
    calc, _ = constraints_mod.instantiate_filer_calc_constraints(templates, unique_numeric)
    return unique_numeric, check.check_constraints(calc, unique_numeric)


if __name__ == "__main__":
    import copy
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
    )

    print(f"Loading {url} ...")
    unique_numeric, checked = _load_clean_inputs(url)
    complete = [c for c in checked if not c.get("partial")]
    print(f"filer-calc constraints: {len(checked)} ({len(complete)} complete, {len(checked) - len(complete)} partial, left out)")

    result = check_satisfiability(unique_numeric, checked)
    print("\nclean filing:", {k: v for k, v in result.items() if k != "unsat_core"})

    # Smoke test only (real injection is inject.py): nudge one total well
    # outside its band and confirm the solver flips to UNSAT and names it.
    target = next(c for c in complete if c["band"] > 0 or c["residual"] == 0)
    corrupted = copy.deepcopy(unique_numeric)
    for fact in corrupted:
        if fact["fact_id"] == target["total_fact_id"]:
            fact["value"] += 2 * target["band"] + 1

    broken = check_satisfiability(corrupted, checked)
    print(f"\nafter shifting the total of {target['constraint_id']} by 2*band+1:")
    print("  status:", broken["status"])
    print("  unsat core:", broken["unsat_core"][:5], f"({len(broken['unsat_core'])} constraints)")
