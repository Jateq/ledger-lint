"""
verify.py — manual full check of ONE local filing .htm (e.g. a test file from inject.py).

    python verify.py datasets/AAPL/000032019325000079/for_llm/case_01.htm
    python verify.py path/to/file.htm --dqc                 # also run DQC rules (slow, minutes)
    python verify.py path/to/file.htm --url <original 10-K URL>   # only if schema files are missing

Arelle needs the filing's schema + linkbase files next to the .htm. This script
looks beside the file, then in _support/ (beside it or one level up, where inject.py puts it),
and downloads them only if you pass --url.

Stages: extract -> filer-calc + ratio-identity constraints -> per-constraint math (exact + rounding band)
-> Z3 global satisfiability -> (optional) DQC.
"""

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

import check
import constraints as constraints_mod
import extract
import inject
import ratios
import solver


def find_support_dir(htm: Path, url):
    schema_ref = re.search(rb'<link:schemaRef\b[^>]*?xlink:href="([^"]+\.xsd)"', htm.read_bytes())
    if not schema_ref:
        raise SystemExit("No schemaRef found: not an inline XBRL filing?")
    schema = schema_ref.group(1).decode()

    for folder in (htm.parent, htm.parent / "_support", htm.parent.parent / "_support"):
        if (folder / schema).exists():
            return folder
    if url:
        folder = htm.parent / "_support"
        inject.download_support_files(htm.read_bytes(), url, folder)
        return folder
    raise SystemExit(f"Schema file {schema} not found next to the file or in _support/. Re-run with --url <original filing URL>.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--url")
    ap.add_argument("--dqc", action="store_true")
    args = ap.parse_args()

    htm = Path(args.file).resolve()
    support = find_support_dir(htm, args.url)

    work = Path(tempfile.mkdtemp(prefix="verify_"))
    for f in support.iterdir():
        if f.suffix in (".xsd", ".xml"):
            shutil.copy(f, work / f.name)
    local = work / "filing.htm"
    shutil.copy(htm, local)

    print(f"Checking {htm.name} ...")
    model = extract.load_filing(str(local))
    records = extract.extract_facts(model)
    unique, counts = extract.dedupe_numeric_facts(records)
    templates = constraints_mod.build_filer_calc_templates(model)
    calc, _ = constraints_mod.instantiate_filer_calc_constraints(templates, unique)
    identities = ratios.identity_constraints(unique)
    checked = check.check_constraints(calc + identities, unique)
    complete = [c for c in checked if not c.get("partial")]

    print(f"\nfacts: {counts['unique_numeric_facts']} unique numeric")
    n_ident = sum(1 for c in complete if c["rule_source"] == "ratio-identity")
    print(f"constraints: {len(complete)} complete = {len(complete) - n_ident} filer-calc + {n_ident} ratio-identity "
          f"(+{len(checked) - len(complete)} partial, ignored)")

    exact_fail = [c for c in complete if not c["pass_zero_tolerance"]]
    band_fail = [c for c in complete if not c["pass_rounding_tolerance"]]
    print(f"  fail exact (zero tolerance): {len(exact_fail)}")
    print(f"  fail within rounding band  : {len(band_fail)}")
    for c in band_fail:
        print(f"    - {c['constraint_id']}: residual {c['residual']}, band {c['band']}")

    z3res = solver.check_satisfiability(unique, checked)
    print(f"\nZ3: {z3res['status'].upper()}")
    for cid in z3res["unsat_core"]:
        print(f"    broken: {cid}")

    if args.dqc:
        print("\nRunning DQC (slow) ...")
        findings, status = constraints_mod.run_dqc_validation(str(local))
        print(f"DQC run: {status}, {len(findings)} violation(s)")
        for f in findings[:10]:
            print("   -", str(f)[:200])

    print("\nVERDICT:", "CLEAN (SAT)" if z3res["status"] == "sat" else "ERROR FOUND (UNSAT)")
    shutil.rmtree(work, ignore_errors=True)
    sys.exit(0 if z3res["status"] == "sat" else 1)


if __name__ == "__main__":
    main()
