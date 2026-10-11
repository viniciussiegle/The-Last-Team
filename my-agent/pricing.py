"""Evidence records and the bid calculation defined by the task brief's rules.

Model-led workers read the websites and report typed records (see RECORD_GUIDE).
This module validates those records, resolves which record governs (exact tender
reference, latest date no later than the scenario date, withdrawn answers) and
does the exact Decimal arithmetic. It never invents a value: anything missing is
reported as a gap and the affected answer fields stay blank.
"""

import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR, InvalidOperation

CENT = Decimal("0.01")

RECORD_GUIDE = """Record kinds and their data fields. Report values exactly as observed. Dates: full ISO 8601 timestamps
including the time (convert the page's time zone to UTC when it is stated). Numbers: strings copied as shown,
keeping a locale hint such as "1.234,5 (de-DE)" when the page gives one or when separators are ambiguous.
When one message or table lists several keys, rates, tariffs or bands, include ALL of them, not only the ones
you think apply. Leave a field out rather than guessing it; use null only for a value proven absent.
Value fields contain only the value (and its locale hint); put any explanation in a separate note field.
authorized is true only when the sender (local part and the bidder's own mail domain) and subject are the
ones the brief names for that topic; otherwise report it with authorized false.
- tender: reference, scenario_date, bidder_mail_domain
- position: position, tender_quantity, unit, requirement_ref, short_text
- qa_quantity: answer, date, position, new_quantity            (a Q&A answer that sets a position's quantity)
- qa_withdrawal: answer, date, withdrawn_answer                 (an answer withdrawing an earlier answer)
- qa_keys: answer, date, position, and any of condition_group, setup_fee_eur, packaging_key, delivery_key,
  setup_pool_key, bonus_plan_key                                (per-position assignments in Q&A answers)
- qa_amendment: answer, date, revision, requirement_ref, attribute (bore|stroke|pressure|mounting|certification), value
- spec_requirement: revision, revision_status (current|superseded|archive), requirement_ref, bore, stroke, pressure,
  mounting, certification, catalog_group, pieces_per_set, usable_stroke (true if stroke is the usable travel
  that needs end reserves), end_reserve_key
- material: material_number, active (true/false), catalog_group, bore, stroke, pressure, mounting, certification,
  available_stock, stock_lead_weeks, production_lead_weeks, pieces_per_carton, surcharge_mass_g,
  allocation_mass_g, metal_index_key
- price_condition: material_number, valid_from, valid_to, currency, fx_key, price_unit (quantity the price refers to),
  number_format (e.g. de-DE or en-US), scales: [{min_qty, price}]
- material_choice: position, material_number, reason            (your matching decision; or no_active_match: true)
- mail: topic, reference, sender, date, authorized (true if sender/subject are authorized by the brief), plus by topic:
    discount_approval: percent, min_order_value_eur (null if unconditional)
    condition_rules: groups: {group_key: base_only|base_and_surcharge|none}
    metal_index: rates: [{key, eur_per_kg, valid_from, valid_to}]
    fx_fixing: rates: [{key, eur_per_unit}]
    packaging_tariff: tariffs: [{key, eur_per_carton}]
    delivery_commitment: key, material_number, max_quantity, lead_time_weeks
    setup_pool: key, fee_eur, allocation_rule (largest_remainder|remainder_to_largest|remainder_to_first|remainder_to_last)
    project_bonus: key, bands: [{from_eur, to_eur, percent}]   (to_eur null for the open top band)
    technical_release: revision, reserves: [{key, mm_per_end}]"""

KINDS = {"tender", "position", "qa_quantity", "qa_withdrawal", "qa_keys", "qa_amendment", "spec_requirement",
         "material", "price_condition", "material_choice", "mail"}
MAIL_TOPICS = {"discount_approval", "condition_rules", "metal_index", "fx_fixing", "packaging_tariff",
               "delivery_commitment", "setup_pool", "project_bonus", "technical_release"}
REQUIRED = {
    "tender": ["reference"], "position": ["position"], "qa_quantity": ["position", "new_quantity"],
    "qa_withdrawal": ["withdrawn_answer"], "qa_keys": ["position"],
    "qa_amendment": ["revision", "requirement_ref", "attribute", "value"],
    "spec_requirement": ["revision", "requirement_ref"], "material": ["material_number"],
    "price_condition": ["material_number", "scales"], "material_choice": ["position"],
    "mail": ["topic"],
}
TOPIC_FIELDS = {
    "discount_approval": ["percent"], "condition_rules": ["groups"], "metal_index": ["rates"],
    "fx_fixing": ["rates"], "packaging_tariff": ["tariffs"],
    "delivery_commitment": ["key", "material_number", "max_quantity", "lead_time_weeks"],
    "setup_pool": ["key", "fee_eur"], "project_bonus": ["key", "bands"], "technical_release": ["reserves"],
}
LIST_FIELDS = {"tariffs": ["key", "eur_per_carton"], "reserves": ["key", "mm_per_end"]}
ATTRIBUTES = ("bore", "stroke", "pressure", "mounting", "certification")
NONE_WORDS = {"", "-", "—", "keine", "keine gefordert", "kein", "none", "n/a", "ohne", "nicht gefordert", "null"}


COUNT, AMOUNT = "count", "amount"


def number(value, number_format: str | None = None, kind: str | None = None) -> Decimal | None:
    """Parse a number as shown on a page, honouring an explicit locale hint.

    Without a hint, kind resolves German/US ambiguity: counts (quantities, stock, grams, weeks) never have
    decimals, so '1.250' and '1,250' are 1250; EUR amounts read '1.250' as 1250 (German grouping) and a
    lone comma as the decimal separator; rates and percentages keep '.' as the decimal separator."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    text = str(value).strip().replace("\u2212", "-")
    hint = re.search(r"\b([a-z]{2}-[A-Z]{2})\b", text)
    if hint:
        number_format = hint.group(1)
    # Only the first number token counts; models sometimes append explanations to values.
    token = re.search(r"-?\d(?:[\d.,'\u00a0\u202f]*\d)?", text)
    if not token:
        return None
    text = re.sub(r"['\u00a0\u202f]", "", token.group(0))
    if not re.search(r"\d", text):
        return None
    fmt = (number_format or "").lower()
    if not fmt and kind in (COUNT, AMOUNT):
        grouped = re.fullmatch(r"\d{1,3}(?:([.,])\d{3})(?:\1\d{3})*", text)
        if grouped and (kind == COUNT or grouped.group(1) == "."):
            return Decimal(text.replace(".", "").replace(",", ""))
    if fmt.startswith(("de", "fr", "es", "it", "nl", "pt", "pl", "cs", "da", "sv", "fi", "nb")):
        text = text.replace(".", "").replace(",", ".")
    elif fmt.startswith(("en", "ja", "zh", "ko")):
        text = text.replace(",", "")
    elif "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def when(value) -> datetime | None:
    """Parse ISO or German dates; a date without time is the start of that day (UTC)."""
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", text)
    if not match:
        german = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:,?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?", text)
        if not german:
            return None
        day, month, year, hour, minute, second = german.groups()
        parts = [year, month, day, hour, minute, second]
    else:
        parts = list(match.groups())
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    hour, minute, second = (int(part) if part else 0 for part in parts[3:6])
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


def day_end(value) -> datetime | None:
    """Inclusive end of a validity period: a bare date covers the whole day."""
    parsed = when(value)
    if parsed and not re.search(r"\d{1,2}:\d{2}", str(value)):
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed


def truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "ja", "aktiv", "active", "freigegeben")


def round2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def norm_text(value) -> str:
    text = re.sub(r"[​-‍﻿]", "", str(value if value is not None else "")).casefold()
    return re.sub(r"\s+", " ", text).strip(" .;")


def answer_id(value) -> str:
    """Q&A answers are referred to as '8152', 'Nr. 8152' or 'Rückfrage 8152': compare the identifier."""
    digits = re.findall(r"\d+", str(value or ""))
    return digits[-1].lstrip("0") or "0" if digits else norm_ref(value)


def norm_ref(value) -> str:
    return re.sub(r"[\s​-‍﻿]", "", str(value or "")).upper()


def same_attribute(attribute: str, required, offered) -> bool:
    if attribute in ("bore", "stroke", "pressure"):
        left, right = number(required, kind=COUNT), number(offered, kind=COUNT)
        return left is not None and left == right
    left, right = norm_text(required), norm_text(offered)
    if left in NONE_WORDS or right in NONE_WORDS:
        return left in NONE_WORDS and right in NONE_WORDS
    return left == right or left.replace(" ", "") == right.replace(" ", "")


def validate(kind: str, data: dict) -> list[str]:
    """Problems with one reported record, returned to the model for correction."""
    problems = []
    if kind not in KINDS:
        return [f"Unknown kind {kind!r}; use one of {sorted(KINDS)}"]
    if not isinstance(data, dict):
        return ["data must be an object"]
    for field in REQUIRED[kind]:
        if data.get(field) in (None, "", []):
            problems.append(f"{kind} needs {field}")
    if kind == "mail":
        if data.get("topic") not in MAIL_TOPICS:
            problems.append(f"mail topic must be one of {sorted(MAIL_TOPICS)}")
        if not data.get("reference") or not data.get("date"):
            problems.append("mail needs the exact tender reference it names and its date")
    if kind == "mail" and data.get("topic") in TOPIC_FIELDS:
        for field in TOPIC_FIELDS[data["topic"]]:
            if data.get(field) in (None, "", [], {}):
                problems.append(f"mail topic {data['topic']} needs {field}")
        for field, keys in LIST_FIELDS.items():
            if isinstance(data.get(field), list) and not all(
                    isinstance(item, dict) and all(item.get(k) not in (None, "") for k in keys) for item in data[field]):
                problems.append(f"each entry of {field} needs {keys}")
    if kind == "price_condition":
        scales = data.get("scales")
        if not isinstance(scales, list) or not all(isinstance(item, dict) and number(item.get("min_qty")) is not None
                                                    and number(item.get("price"), data.get("number_format")) is not None
                                                    for item in scales):
            problems.append("scales must be a list of {min_qty, price} with numbers")
        elif not data.get("number_format") and any(
                re.search(r"\d[.,]\d", str(item.get(k))) and not re.search(r"[a-z]{2}-[A-Z]{2}", str(item.get(k)))
                for item in scales for k in ("min_qty", "price")):
            problems.append("state number_format as the page shows it (e.g. de-DE or en-US): '.' and ',' are ambiguous")
    if kind == "qa_amendment" and data.get("attribute") not in ATTRIBUTES:
        problems.append(f"attribute must be one of {ATTRIBUTES}")
    return problems


SINGLE_ENTRY = {"packaging_tariff": ("tariffs", ("key", "eur_per_carton")),
                "metal_index": ("rates", ("key", "eur_per_kg", "valid_from", "valid_to")),
                "fx_fixing": ("rates", ("key", "eur_per_unit")),
                "technical_release": ("reserves", ("key", "mm_per_end"))}


def normalize_mail(data: dict) -> dict:
    """Accept common shapes: fields nested under the topic name, or one list entry given flat."""
    data = dict(data)
    nested = data.get(data.get("topic"))
    if isinstance(nested, dict):
        for field, value in nested.items():
            data.setdefault(field, value)
    elif isinstance(nested, list) and data.get("topic") in SINGLE_ENTRY:
        data.setdefault(SINGLE_ENTRY[data["topic"]][0], nested)
    if data.get("topic") in SINGLE_ENTRY:
        field, keys = SINGLE_ENTRY[data["topic"]]
        if isinstance(data.get(field), dict):
            data[field] = [data[field]]
        if not data.get(field) and data.get("key") and data.get(keys[1]) not in (None, ""):
            data[field] = [{k: data.get(k) for k in keys if data.get(k) not in (None, "")}]
    return data


class Evidence:
    """The shared board of typed records reported by all workers."""

    def __init__(self):
        self.records: list[dict] = []

    def add(self, kind: str, data: dict, source: str = "") -> list[str]:
        if kind == "mail" and isinstance(data, dict):
            data = normalize_mail(data)
        problems = validate(kind, data)
        if not problems:
            self.records.append({"kind": kind, "data": data, "source": source})
        return problems

    def of(self, kind: str) -> list[dict]:
        return [record["data"] for record in self.records if record["kind"] == kind]

    def summary(self, start: int = 0) -> list[str]:
        lines = []
        for record in self.records[start:]:
            data = {key: value for key, value in record["data"].items() if value not in (None, "", [])}
            lines.append(f"{record['kind']}: " + ", ".join(f"{k}={v}" for k, v in data.items())[:600])
        return lines


def items(value) -> list[dict]:
    """List entries that are objects; tolerates a single object or malformed entries."""
    if isinstance(value, dict):
        return [value]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def mapping(value) -> dict:
    """A key -> value mapping; tolerates a list of {key, rule/value} objects."""
    if isinstance(value, dict):
        return value
    result = {}
    for item in items(value):
        key = item.get("key") or item.get("group")
        rule = item.get("rule") or item.get("value") or item.get("applies_to")
        if key and rule:
            result[key] = rule
    return result


def merged(rows: list[dict], key: str) -> dict:
    """Merge partial records with the same key; later non-empty values win."""
    result = {}
    for row in rows:
        identifier = norm_ref(row.get(key))
        if not identifier:
            continue
        target = result.setdefault(identifier, {})
        for field, value in row.items():
            if value not in (None, "", []):
                target[field] = value
    return result


def calculate_bid(evidence: Evidence, brief_reference: str = "", brief_scenario: str = "") -> dict:
    """Assemble the answer from the evidence. Returns {answer, gaps, details}."""
    gaps: list[str] = []
    tender = merged(evidence.of("tender"), "reference")
    reference = brief_reference or (next(iter(tender.values()), {}).get("reference") if tender else "")
    tender_info = tender.get(norm_ref(reference), next(iter(tender.values()), {}) if tender else {})
    scenario = when(brief_scenario) or when(tender_info.get("scenario_date"))
    if not scenario:
        gaps.append("scenario date")
        scenario = datetime.now(timezone.utc)
    ref = norm_ref(reference)

    def applicable(row):
        dated = when(row.get("date"))
        return (norm_ref(row.get("reference")) == ref and row.get("authorized") is not False
                and (dated is None or dated <= scenario))

    def latest_mail(topic):
        rows = [row for row in evidence.of("mail") if row.get("topic") == topic and applicable(row)]
        return sorted(rows, key=lambda row: when(row.get("date")) or datetime.min.replace(tzinfo=timezone.utc))

    # Specification: the current revision, scoped amendments, end reserves.
    requirements = defaultdict(dict)
    for row in evidence.of("spec_requirement"):
        status = norm_text(row.get("revision_status"))
        requirements[norm_ref(row.get("revision"))].setdefault("_status", set()).add(status)
        target = requirements[norm_ref(row.get("revision"))].setdefault(norm_ref(row.get("requirement_ref")), {})
        target.update({k: v for k, v in row.items() if v not in (None, "", [])})
    current = [revision for revision, rows in requirements.items()
               if rows["_status"] & {"current", "gültig", "valid", "aktuell", "active"}]
    if len(current) != 1:
        gaps.append("current specification revision" + (f" (candidates {current})" if current else ""))
    revision = current[0] if current else None
    reserves = {}
    for row in latest_mail("technical_release"):
        if revision and norm_ref(row.get("revision")) not in ("", revision):
            continue
        for item in items(row.get("reserves")):
            reserves[norm_ref(item.get("key"))] = number(item.get("mm_per_end"))

    withdrawn = {answer_id(row.get("withdrawn_answer")) for row in evidence.of("qa_withdrawal")
                 if (when(row.get("date")) or scenario) <= scenario}

    def required_for(requirement_ref):
        spec = dict(requirements.get(revision, {}).get(norm_ref(requirement_ref), {})) if revision else {}
        if not spec:
            return None, f"current requirement {requirement_ref}"
        for attribute in ATTRIBUTES:
            amendments = [row for row in evidence.of("qa_amendment")
                          if norm_ref(row.get("revision")) == revision
                          and norm_ref(row.get("requirement_ref")) == norm_ref(requirement_ref)
                          and row.get("attribute") == attribute
                          and answer_id(row.get("answer")) not in withdrawn
                          and (when(row.get("date")) or scenario) <= scenario]
            if amendments:
                spec[attribute] = max(amendments, key=lambda row: when(row.get("date")) or scenario)["value"]
        if str(spec.get("usable_stroke")).lower() in ("true", "1", "yes", "ja"):
            reserve = reserves.get(norm_ref(spec.get("end_reserve_key")))
            if reserve is None or number(spec.get("stroke")) is None:
                return None, f"end reserve {spec.get('end_reserve_key')} for {requirement_ref}"
            spec["stroke"] = str(number(spec["stroke"]) + 2 * reserve)
        return spec, None

    # Materials and their conditions.
    materials = merged(evidence.of("material"), "material_number")
    conditions = defaultdict(list)
    for row in evidence.of("price_condition"):
        conditions[norm_ref(row.get("material_number"))].append(row)
    choices = merged(evidence.of("material_choice"), "position")

    positions = merged(evidence.of("position"), "position")
    if not positions:
        gaps.append("tender positions")
    keys = defaultdict(dict)
    for row in sorted(evidence.of("qa_keys"), key=lambda row: when(row.get("date")) or scenario):
        if answer_id(row.get("answer")) in withdrawn or (when(row.get("date")) or scenario) > scenario:
            continue
        keys[norm_ref(row.get("position"))].update({k: v for k, v in row.items() if v not in (None, "", [])})

    lines, no_match = [], []
    for position_id in sorted(positions):
        position = positions[position_id]
        line = {"position": position.get("position", position_id), "gaps": []}
        changes = [row for row in evidence.of("qa_quantity")
                   if norm_ref(row.get("position")) == position_id and answer_id(row.get("answer")) not in withdrawn
                   and (when(row.get("date")) or scenario) <= scenario]
        quantity = number(position.get("tender_quantity"), kind=COUNT)
        if changes:
            latest = max(changes, key=lambda row: (when(row.get("date")) or scenario, str(row.get("answer"))))
            quantity = number(latest.get("new_quantity"), kind=COUNT)
        spec, problem = required_for(position.get("requirement_ref"))
        if problem:
            line["gaps"].append(problem)
        if quantity is not None:
            unit = norm_text(position.get("unit"))
            pieces = unit in ("", "stk", "stck", "stück", "stueck", "st", "pcs", "pc", "piece", "pieces", "ea", "each")
            per_set = number((spec or {}).get("pieces_per_set"), kind=COUNT)
            if not pieces and per_set is not None:
                quantity *= per_set
            elif not pieces and re.search(r"satz|sätze|set|kit|paar|pair|pack|gebinde", unit):
                line["gaps"].append(f"pieces per {position.get('unit')}")
                quantity = None
        line["quantity"] = int(quantity) if quantity is not None and quantity == quantity.to_integral() else None
        if line["quantity"] is None:
            line["gaps"].append("quantity")

        # Material: exact match on all attributes; the model's choice resolves ties or formatting.
        material_number = None
        if spec:
            fits = [key for key, material in materials.items()
                    if all(same_attribute(a, spec.get(a), material.get(a)) for a in ATTRIBUTES)]
            active = [key for key in fits if truthy(materials[key].get("active"))]
            unknown = [key for key in fits if materials[key].get("active") in (None, "")]
            choice = norm_ref(choices.get(position_id, {}).get("material_number"))
            if len(active) == 1:
                material_number = active[0]
            elif choice in active:
                material_number = choice
            elif not active and choice in unknown:
                material_number = choice
                line["gaps"].append(f"active status of {choice} unverified")
            elif not active and len(unknown) == 1:
                material_number = unknown[0]
                line["gaps"].append(f"active status of {unknown[0]} unverified")
            elif not fits and choice in materials and truthy(materials[choice].get("active")):
                material_number = choice  # attribute formatting differed; trust the model's comparison
            group = norm_ref(spec.get("catalog_group"))
            searched = [key for key, material in materials.items()
                        if group and norm_ref(material.get("catalog_group")) == group]
            # no_bid needs ERP evidence: the group's materials were reported and none is an active match.
            if (not material_number and truthy(choices.get(position_id, {}).get("no_active_match"))
                    and searched and not unknown):
                no_match.append(position_id)
        line["material_number"] = materials.get(material_number, {}).get("material_number", material_number)
        line["required"] = {a: (spec or {}).get(a) for a in ATTRIBUTES}
        if not material_number:
            line["gaps"].append("matching active material")
        line["_material"] = materials.get(material_number, {})
        line["_keys"] = keys.get(position_id, {})
        lines.append(line)

    # Base prices: valid condition on the scenario date, highest scale reached, FX to EUR.
    fx_rates = {}
    for row in latest_mail("fx_fixing"):
        for item in items(row.get("rates")):
            fx_rates[norm_ref(item.get("key"))] = number(item.get("eur_per_unit"))
    for line in lines:
        material = line["_material"]
        if not material or line["quantity"] is None:
            continue
        valid = []
        for row in conditions.get(norm_ref(material.get("material_number")), []):
            start, end = when(row.get("valid_from")), day_end(row.get("valid_to"))
            if (start is None or start <= scenario) and (end is None or scenario <= end):
                valid.append(row)
        if not valid:
            line["gaps"].append("price condition valid on the scenario date")
            continue
        condition = valid[-1]
        fmt = condition.get("number_format")
        scales = [(number(item.get("min_qty"), fmt, COUNT), number(item.get("price"), fmt)) for item in items(condition["scales"])]
        reached = [scale for scale in scales if scale[0] is not None and scale[0] <= line["quantity"]]
        if not reached:
            line["gaps"].append("quantity scale")
            continue
        price = max(reached, key=lambda scale: scale[0])[1] / (number(condition.get("price_unit"), fmt, COUNT) or Decimal(1))
        currency = norm_text(condition.get("currency") or "eur")
        if re.match(r"(eur\b|€)", currency) or currency == "euro":
            rate = Decimal(1)
        else:
            rate = fx_rates.get(norm_ref(condition.get("fx_key")))
            if rate is None:
                line["gaps"].append(f"FX rate {condition.get('fx_key')}")
                continue
        line["base"] = round2(price * rate)

    # Discount: latest exact-reference approval; conditional on the undiscounted order value.
    approvals = latest_mail("discount_approval")
    discount = Decimal(0)
    undiscounted = round2(sum((Decimal(line["quantity"]) * line["base"] for line in lines if "base" in line),
                              Decimal(0)))
    if approvals:
        approval = approvals[-1]
        discount = number(approval.get("percent")) or Decimal(0)
        minimum = number(approval.get("min_order_value_eur"), kind=AMOUNT)
        if minimum is not None:
            if any("base" not in line for line in lines):
                gaps.append("all base prices for the discount threshold")
            if undiscounted < minimum:
                discount = Decimal(0)
    rules = {}
    for row in latest_mail("condition_rules"):
        rules.update({norm_ref(k): norm_text(v) for k, v in mapping(row.get("groups")).items()})

    # Metal surcharge, packaging, delivery.
    metal = {}
    for row in latest_mail("metal_index"):
        for item in items(row.get("rates")):
            start, end = when(item.get("valid_from")), day_end(item.get("valid_to"))
            if (start is None or start <= scenario) and (end is None or scenario <= end):
                metal[norm_ref(item.get("key"))] = number(item.get("eur_per_kg"))
    tariffs = {}
    for row in latest_mail("packaging_tariff"):
        for item in items(row.get("tariffs")):
            tariffs[norm_ref(item.get("key"))] = number(item.get("eur_per_carton"), kind=AMOUNT)
    commitments = {}
    for row in latest_mail("delivery_commitment"):
        commitments[norm_ref(row.get("key"))] = row
    demand = defaultdict(int)
    for line in lines:
        if line["material_number"] and line["quantity"] is not None:
            demand[norm_ref(line["material_number"])] += line["quantity"]

    for line in lines:
        material, assigned = line["_material"], line["_keys"]
        if not material or line["quantity"] is None:
            continue
        quantity = Decimal(line["quantity"])
        mass = number(material.get("surcharge_mass_g"), kind=COUNT)
        if mass is None or mass == 0:
            line["mass_surcharge"] = Decimal(0)
        else:
            rate = metal.get(norm_ref(material.get("metal_index_key")))
            if rate is None:
                line["gaps"].append(f"metal index {material.get('metal_index_key')}")
            else:
                line["mass_surcharge"] = mass / 1000 * rate
        line["setup"] = number(assigned.get("setup_fee_eur"), kind=AMOUNT) or Decimal(0)
        key = norm_ref(assigned.get("packaging_key"))
        carton = number(material.get("pieces_per_carton"), kind=COUNT)
        if not key:
            line["packaging"] = Decimal(0)
        elif tariffs.get(key) is None or not carton:
            line["gaps"].append(f"packaging tariff {assigned.get('packaging_key')} or carton size")
        else:
            line["packaging"] = Decimal(math.ceil(quantity / carton)) * tariffs[key]
        pooled = demand[norm_ref(line["material_number"])]
        commitment = commitments.get(norm_ref(assigned.get("delivery_key")))
        if assigned.get("delivery_key") and not commitment:
            line["gaps"].append(f"delivery commitment {assigned.get('delivery_key')}")
        stock = number(material.get("available_stock"), kind=COUNT)
        if (commitment and norm_ref(commitment.get("material_number")) == norm_ref(line["material_number"])
                and number(commitment.get("max_quantity"), kind=COUNT) is not None
                and pooled <= number(commitment.get("max_quantity"), kind=COUNT)
                and number(commitment.get("lead_time_weeks"), kind=COUNT) is not None):
            line["lead_time_weeks"] = int(number(commitment["lead_time_weeks"], kind=COUNT))
        elif stock is not None and pooled <= stock and number(material.get("stock_lead_weeks"), kind=COUNT) is not None:
            line["lead_time_weeks"] = int(number(material["stock_lead_weeks"], kind=COUNT))
        elif stock is not None and number(material.get("production_lead_weeks"), kind=COUNT) is not None:
            line["lead_time_weeks"] = int(number(material["production_lead_weeks"], kind=COUNT))
        else:
            line["gaps"].append("stock or lead times")

    # Shared setup pools: allocate the pool fee in cents by quantity x allocation mass.
    pools = {norm_ref(row.get("key")): row for row in latest_mail("setup_pool")}
    members = defaultdict(list)
    for line in lines:
        if line["_keys"].get("setup_pool_key") and line["quantity"] is not None:
            members[norm_ref(line["_keys"]["setup_pool_key"])].append(line)
    for key, group in members.items():
        pool = pools.get(key)
        weights = [Decimal(line["quantity"]) * (number(line["_material"].get("allocation_mass_g"), kind=COUNT) or Decimal(0))
                   for line in group]
        if not pool or number(pool.get("fee_eur"), kind=AMOUNT) is None or not all(weights):
            for line in group:
                line["gaps"].append(f"setup pool {key}")
            continue
        shares = allocate(number(pool["fee_eur"], kind=AMOUNT), weights, [line["position"] for line in group],
                          norm_text(pool.get("allocation_rule") or "largest_remainder"))
        for line, share in zip(group, shares):
            line["shared_setup"] = share

    # Project bonus: marginal bands on eligible undiscounted goods, allocated in cents.
    plans = {norm_ref(row.get("key")): row for row in latest_mail("project_bonus")}
    bonus_members = defaultdict(list)
    for line in lines:
        if line["_keys"].get("bonus_plan_key"):
            bonus_members[norm_ref(line["_keys"]["bonus_plan_key"])].append(line)
    for key, group in bonus_members.items():
        plan = plans.get(key)
        if not plan or not plan.get("bands") or any("base" not in line for line in group):
            for line in group:
                line["gaps"].append(f"project bonus {key}")
            continue
        values = [Decimal(line["quantity"]) * line["base"] for line in group]
        total = sum(values, Decimal(0))
        credit = Decimal(0)
        for band in items(plan["bands"]):
            low = number(band.get("from_eur"), kind=AMOUNT) or Decimal(0)
            high = number(band.get("to_eur"), kind=AMOUNT)
            portion = (min(total, high) if high is not None else total) - low
            if portion > 0:
                credit += portion * (number(band.get("percent")) or Decimal(0)) / 100
        shares = allocate(round2(credit), values, [line["position"] for line in group], "largest_remainder")
        for line, share in zip(group, shares):
            line["bonus"] = share

    # Final unit prices and the answer.
    factor = 1 - discount / 100
    answer_positions, total = [], Decimal(0)
    for line in lines:
        if line["quantity"] is None or not line["material_number"] or "base" not in line:
            continue
        quantity = Decimal(line["quantity"])
        group = norm_ref(line["_keys"].get("condition_group"))
        clause = rules.get(group, "base_only") if group else "base_only"
        if group and group not in rules:
            line["gaps"].append(f"condition rule {group}")
        mass = line.get("mass_surcharge", Decimal(0))
        base = line["base"] * factor if clause in ("base_only", "base_and_surcharge") else line["base"]
        mass_part = mass * factor if clause == "base_and_surcharge" else mass
        fees = line.get("setup", 0) + line.get("shared_setup", 0) + line.get("packaging", 0) - line.get("bonus", 0)
        unit_price = round2(base + mass_part + fees / quantity)
        entry = {"position": line["position"], "material_number": line["material_number"],
                 "quantity": line["quantity"], "unit_price": float(unit_price),
                 "base_unit_price": float(line["base"])}
        if "lead_time_weeks" in line:
            entry["lead_time_weeks"] = line["lead_time_weeks"]
        if "mass_surcharge" in line and (not line["_keys"].get("setup_pool_key") or "shared_setup" in line):
            entry["surcharge_per_unit"] = float(round2(mass + (line.get("setup", 0) + line.get("shared_setup", 0))
                                                       / quantity))
        if "packaging" in line:
            entry["packaging_per_unit"] = float(round2(line["packaging"] / quantity))
        if not line["_keys"].get("bonus_plan_key"):
            entry["rebate_total"] = 0.0
        elif "bonus" in line:
            entry["rebate_total"] = float(line["bonus"])
        total += quantity * unit_price
        answer_positions.append(entry)
    for line in lines:
        gaps.extend(f"{line['position']}: {gap}" for gap in line["gaps"])
    decision = "no_bid" if no_match else "bid"
    answer = {"tender_reference": reference, "decision": decision, "currency": "EUR",
              "discount_percent": float(discount) if decision == "bid" else
              (0.0 if approvals and number(approvals[-1].get("min_order_value_eur")) is not None
               else float(discount)),
              "positions": answer_positions if decision == "bid" else [],
              "total_net": float(round2(total)) if decision == "bid" else 0.0}
    details = [{key: (str(value) if isinstance(value, Decimal) else value) for key, value in line.items()
                if not key.startswith("_")} for line in lines]
    return {"answer": answer, "gaps": gaps, "details": details,
            "omitted": [line["position"] for line in lines
                        if line["position"] not in {entry["position"] for entry in answer_positions}],
            "undiscounted_order_value": str(undiscounted), "no_match": no_match}


def allocate(amount: Decimal, weights: list[Decimal], labels: list[str], rule: str) -> list[Decimal]:
    """Split an amount in cents proportionally to weights."""
    cents = int((amount / CENT).to_integral_value(rounding=ROUND_HALF_UP))
    total = sum(weights, Decimal(0))
    exact = [Decimal(cents) * weight / total for weight in weights]
    if "largest" in rule and "remainder" in rule and "to" not in rule:
        floors = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in exact]
        order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - floors[i]), labels[i]))
        for i in order[:cents - sum(floors)]:
            floors[i] += 1
        return [Decimal(value) * CENT for value in floors]
    rounded = [int(value.to_integral_value(rounding=ROUND_HALF_UP)) for value in exact]
    rest = cents - sum(rounded)
    if "largest" in rule:
        target = max(range(len(weights)), key=lambda i: (weights[i], [-ord(c) for c in labels[i]]))
    elif "first" in rule:
        target = min(range(len(weights)), key=lambda i: labels[i])
    else:
        target = max(range(len(weights)), key=lambda i: labels[i])
    rounded[target] += rest
    return [Decimal(value) * CENT for value in rounded]
