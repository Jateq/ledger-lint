"""
extract.py — Step 2 of the pipeline: load a filing into Arelle and pull out
every fact plus its context/unit/label/section info.

Input: a single filing entry-point URL (from fetch.py).
Output: a flat list of fact records (dicts) ready for constraints.py.

Arelle, given that one URL, auto-resolves schemaRef/linkbaseRef and pulls
down the calculation linkbase, presentation linkbase, label linkbase, and
the taxonomy itself — we don't fetch any of those ourselves.
"""

from decimal import Decimal

from arelle import Cntlr, XbrlConst

USER_AGENT = "Temirlan Yerlanuly research tool (temirlan.eraly1@gmail.com)"


def load_filing(url: str):
    """
    Load a filing (and everything it references) into Arelle.
    Returns an arelle.ModelXbrl.ModelXbrl instance.
    This is the slow step (network + parsing) — call it once per filing.
    """
    controller = Cntlr.Cntlr(logFileName=None)
    controller.webCache.httpUserAgent = USER_AGENT
    model = controller.modelManager.load(url)
    if model is None:
        raise RuntimeError(f"Arelle failed to load filing: {url}")
    return model


def get_all_facts(model) -> list:
    """Return the raw list of ModelFact objects in the filing."""
    return list(model.facts)


def decode_context(context) -> dict | None:
    """
    Turn an Arelle context object into a plain dict: entity, period, dimensions.
    """
    if context is None:
        return None

    if context.isInstantPeriod:
        period = {
            "type": "instant",
            "instant": context.instantDatetime.isoformat() if context.instantDatetime else None,
        }
    elif context.isStartEndPeriod:
        period = {
            "type": "duration",
            "start": context.startDatetime.isoformat() if context.startDatetime else None,
            "end": context.endDatetime.isoformat() if context.endDatetime else None,
        }
    else:
        period = {"type": "forever"}

    _, entity_id = context.entityIdentifier

    dimensions = {}
    for dim_qname, dim_value in context.qnameDims.items():
        if dim_value.isExplicit:
            dimensions[str(dim_qname)] = str(dim_value.memberQname)
        else:
            typed_member = dim_value.typedMember
            dimensions[str(dim_qname)] = typed_member.text if typed_member is not None else None

    return {
        "context_id": context.id,
        "entity": entity_id,
        "period": period,
        "dimensions": dimensions,
    }


def decode_unit(unit) -> str | None:
    """
    Turn an Arelle unit object into a human-readable string, e.g. 'iso4217:USD'
    or 'iso4217:USD/shares' for divide units.
    """
    if unit is None:
        return None

    numerators, denominators = unit.measures
    num_str = "*".join(str(m) for m in numerators) if numerators else None
    if denominators:
        den_str = "*".join(str(m) for m in denominators)
        return f"{num_str}/{den_str}"
    return num_str


def numeric_dedup_key(fact) -> str | None:
    """
    Key used to group numeric fact occurrences that represent the same
    underlying fact: same concept + equivalent context + equivalent unit.
    Uses Arelle's own dimension-aware context hash and unit hash rather than
    raw context_id/unit_id, since two different context_ids in the instance
    can describe the same period/entity/dimensions.
    Returns None for nonnumeric facts (dedup only applies to numeric facts).
    """
    if not fact.isNumeric:
        return None

    ctx_hash = fact.context.contextDimAwareHash if fact.context is not None else None
    unit_hash = fact.unit.hash if fact.unit is not None else None
    return f"{fact.qname}|{ctx_hash}|{unit_hash}"


def build_concept_sections_map(model) -> dict:
    """
    Map each concept qname (as a string) to the list of "sections" it
    appears in, using the presentation linkbase. A section is one extended
    link role (ELR) — e.g. "CONSOLIDATED STATEMENTS OF OPERATIONS" — taken
    from that role's human-readable definition.
    """
    presentation_rels = model.relationshipSet(XbrlConst.parentChild)

    sections_by_concept: dict = {}
    for rel in presentation_rels.modelRelationships:
        concept = rel.toModelObject
        if concept is None:
            continue

        role_types = model.roleTypes.get(rel.linkrole)
        section_label = role_types[0].definition if role_types else rel.linkrole

        sections_by_concept.setdefault(str(concept.qname), set()).add(section_label)

    return {concept: sorted(labels) for concept, labels in sections_by_concept.items()}


def stable_fact_id(fact) -> str:
    """
    The fact's own id attribute from the filing document (e.g. "f-1"), which
    is the same on every run. Falls back to Arelle's internal object id
    (only stable within one load) for the rare fact with no id attribute.
    """
    return fact.id if fact.id else f"obj{fact.objectId()}"


def build_fact_record(fact, sections_map: dict) -> dict:
    """Turn one Arelle ModelFact into a plain-dict fact record."""
    concept = fact.concept
    qname_str = str(fact.qname)
    is_numeric = fact.isNumeric

    return {
        "fact_id": stable_fact_id(fact),
        "source_line": fact.sourceline,
        "concept": qname_str,
        "label": concept.label() if concept is not None else None,
        "type": "numeric" if is_numeric else "nonnumeric",
        "value": fact.xValue if is_numeric else fact.value,
        "decimals": fact.decimals if is_numeric else None,
        "unit": decode_unit(fact.unit) if is_numeric else None,
        "context_ref": decode_context(fact.context),
        "sections": sections_map.get(qname_str, []),
        "is_nil": fact.isNil,
        "numeric_key": numeric_dedup_key(fact),
    }


def extract_facts(model) -> list:
    """
    Extract every fact in the filing as a flat list of fact records
    (one record per occurrence — not deduped yet).
    """
    sections_map = build_concept_sections_map(model)
    return [build_fact_record(fact, sections_map) for fact in get_all_facts(model)]


def dedupe_numeric_facts(records: list) -> tuple[list, dict]:
    """
    Collapse numeric fact occurrences that share the same numeric_key
    (concept + equivalent context + equivalent unit) into one unique fact,
    keeping every source occurrence's fact_id linked under "occurrences".

    Returns (unique_numeric_facts, counts) where counts has:
      total_fact_occurrences   — every fact in the filing (numeric + nonnumeric)
      numeric_fact_occurrences — every numeric fact occurrence, pre-dedup
      unique_numeric_facts     — count after dedup
    """
    numeric_records = [r for r in records if r["type"] == "numeric"]

    unique_by_key: dict = {}
    for record in numeric_records:
        key = record["numeric_key"]
        if key not in unique_by_key:
            unique_by_key[key] = {
                **record,
                "occurrences": [record["fact_id"]],
                "occurrence_lines": [record["source_line"]],
                "_group": [record],
            }
        else:
            group = unique_by_key[key]
            group["occurrences"].append(record["fact_id"])
            group["occurrence_lines"].append(record["source_line"])
            group["_group"].append(record)

    unique_numeric_facts = list(unique_by_key.values())
    for unique in unique_numeric_facts:
        _flag_conflict(unique)

    counts = {
        "total_fact_occurrences": len(records),
        "numeric_fact_occurrences": len(numeric_records),
        "nonnumeric_fact_occurrences": len(records) - len(numeric_records),
        "unique_numeric_facts": len(unique_numeric_facts),
        "conflicting_unique_facts": sum(1 for u in unique_numeric_facts if u["has_conflict"]),
    }
    return unique_numeric_facts, counts


def _flag_conflict(unique: dict) -> None:
    """
    Merged occurrences share concept + context + unit, so they should carry
    the same value. Mark the merged fact if they don't — has_conflict — and
    whether the disagreement is small enough to be explained by the
    occurrences' own rounding (conflict_within_rounding).
    """
    group = unique.pop("_group")
    values = [r["value"] for r in group if r["value"] is not None]
    distinct = sorted(set(values))

    unique["has_conflict"] = len(distinct) > 1
    unique["conflicting_values"] = distinct if len(distinct) > 1 else []
    unique["conflict_within_rounding"] = False

    if len(distinct) > 1:
        decimals = [
            int(r["decimals"]) for r in group
            if r["decimals"] not in (None, "INF")
        ]
        if decimals:
            tolerance = 2 * Decimal(10) ** Decimal(-min(decimals)) * Decimal("0.5")
            unique["conflict_within_rounding"] = (distinct[-1] - distinct[0]) <= tolerance


if __name__ == "__main__":
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"
    )

    print(f"Loading {url} ...")
    model = load_filing(url)

    records = extract_facts(model)
    unique_numeric, counts = dedupe_numeric_facts(records)

    print("\n--- counts ---")
    for k, v in counts.items():
        print(f"{k}: {v}")

    dupe = next((f for f in unique_numeric if len(f["occurrences"]) > 1), None)
    if dupe:
        print("\n--- example fact with multiple occurrences ---")
        print("concept:", dupe["concept"])
        print("context_ref:", dupe["context_ref"])
        print("unit:", dupe["unit"])
        print("value:", dupe["value"])
        print("occurrences:", dupe["occurrences"])

    sectioned = next((f for f in unique_numeric if f["sections"]), None)
    if sectioned:
        print("\n--- example fact with sections ---")
        print("concept:", sectioned["concept"])
        print("sections:", sectioned["sections"])
