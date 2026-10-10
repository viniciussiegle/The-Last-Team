from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_CEILING

from ..evidence.resolver import normalized, resolve, timestamp
from .allocation import largest_remainder, marginal_credit
from .money import D, cents


@dataclass
class Calculation:
    answer: dict
    trace: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    complete: bool = False


def on_date(value):
    return date.fromisoformat(str(value)[:10])


def calculate(ledger, context, include_setup_in_surcharge=True, core_only=False):
    resolved = resolve(ledger, context)
    answer = {"tender_reference": context["reference"], "decision": "bid", "currency": "EUR",
              "discount_percent": Decimal(0), "positions": [], "total_net": Decimal(0)}
    calc = Calculation(answer, gaps=list(resolved.gaps))
    scenario = on_date(context["scenario_date"])
    def trace(position, rule, inputs, output):
        calc.trace.append({"position": position, "rule": rule, "inputs": inputs, "output": output})
    def payload(kind):
        record = resolved.mails.get(kind)
        return record.payload if record else {}
    def key_for(position, kind):
        entries = [q for q in resolved.qa if q.position_id == position and q.kind == kind]
        return entries[-1].payload.get("key") if entries else None
    def missing(message):
        calc.gaps.append(message)
    if not resolved.positions:
        missing("Find and record every tender position, original quantity, unit and requirement reference")
    if not resolved.spec:
        return calc
    requirements = {r.requirement_id: r for r in resolved.spec.requirements}
    material_records = {}
    for material in ledger.of("Material"):
        prior = material_records.get(material.number)
        # Optional unknowns on another tab do not retract previously observed facts.
        updates = {key: value for key, value in material.model_dump().items() if value is not None}
        material_records[material.number] = prior.model_copy(update=updates) if prior else material
    materials = list(material_records.values())
    lines = {}
    pooled = {}
    for position in resolved.positions:
        pid = position.position_id
        requirement = requirements.get(position.requirement_ref)
        if requirement is None:
            missing(f"Position {pid}: governing requirement {position.requirement_ref}")
            continue
        attributes = requirement.model_dump()
        for q in resolved.qa:
            if (q.kind == "technical_amendment" and q.revision_ref == resolved.spec.revision_id
                    and q.requirement_ref == requirement.requirement_id):
                for attr, value in q.payload.get("attributes", {}).items():
                    if attr in ("bore", "stroke", "pressure", "mounting", "certification", "pieces_per_set", "stroke_kind"):
                        attributes[attr] = value
        quantity = position.quantity
        changes = [q for q in resolved.qa if q.kind == "quantity_change" and q.position_id == pid]
        if changes:
            quantity = D(changes[-1].payload["quantity"])
        if position.unit == "set":
            if attributes.get("pieces_per_set") is None:
                missing(f"Position {pid}: pieces per set")
                continue
            quantity *= D(attributes["pieces_per_set"])
        if quantity <= 0 or quantity != quantity.to_integral_value():
            missing(f"Position {pid}: final piece quantity must be positive and integral")
            continue
        trace(pid, "quantity", {"original": position.quantity, "unit": position.unit}, quantity)
        if attributes["stroke_kind"] == "usable":
            reserve = next((r for r in payload("technikfreigabe").get("reserves", [])
                            if r["revision"] == resolved.spec.revision_id and r["requirement"] == requirement.requirement_id), None)
            if reserve is None:
                missing(f"Position {pid}: revision-specific technical per-end reserve")
                continue
            attributes["stroke"] = D(attributes["stroke"]) + 2 * D(reserve["per_end"])
        matches = [m for m in materials if m.status == "active" and normalized(m.katalog_group) == normalized(requirement.katalog_group)
                   and all(D(getattr(m, name)) == D(attributes[name]) for name in ("bore", "stroke", "pressure"))
                   and all(normalized(getattr(m, name)) == normalized(attributes[name]) for name in ("mounting", "certification"))]
        if not matches:
            observed_materials = {m.number for m in materials if normalized(m.katalog_group) == normalized(requirement.katalog_group)}
            coverage = [c for c in ledger.of("SearchCoverage") if c.katalog_group == requirement.katalog_group and c.complete
                        and set(c.candidate_materials) <= observed_materials]
            if coverage:
                answer.update(decision="no_bid", positions=[], total_net=Decimal(0), discount_percent=Decimal(0))
                calc.complete = True
                trace(pid, "no_matching_active_material", attributes, "no_bid")
                return calc
            missing(f"Position {pid}: search all active candidates in catalogue group {requirement.katalog_group}; compare {attributes}")
            continue
        if len(matches) > 1:
            missing(f"Position {pid}: multiple exact active materials; verify distinguishing attributes/status")
            continue
        material = matches[0]
        # Known demand consumes shared stock even when that line's price is missing.
        pooled[material.number] = pooled.get(material.number, Decimal(0)) + quantity
        prices = [p for p in ledger.of("PriceCondition") if p.material == material.number and on_date(p.valid_from) <= scenario <= on_date(p.valid_to)]
        if not prices:
            missing(f"Material {material.number}: full condition valid on scenario date with price unit/currency and all scales")
            continue
        condition = prices[-1]
        scales = [s for s in condition.scales if s.min_qty <= quantity]
        if not scales:
            missing(f"Material {material.number}: quantity scale at or below {quantity}")
            continue
        base = max(scales, key=lambda s: s.min_qty).price / condition.price_unit_qty
        if condition.currency.upper() != "EUR":
            fx = payload("devisenfixing").get("rates", {}).get(condition.fx_key)
            if fx is None:
                missing(f"Material {material.number}: authorized FX rate for {condition.fx_key}")
                continue
            base *= D(fx)
        base = cents(base)
        line = {"position": position, "material": material, "qty": quantity, "base": base,
                "goods": quantity * base, "shared_setup": Decimal(0), "carton_cost": Decimal(0), "bonus": Decimal(0)}
        lines[pid] = line
        trace(pid, "material_and_base", {"material": material.number, "condition": condition.model_dump()}, base)
    approval = payload("discount_approval")
    goods_total = cents(sum((line["goods"] for line in lines.values()), Decimal(0)))
    discount = D(approval.get("percent", 0))
    if approval.get("min_order_value") is not None:
        if len(lines) != len(resolved.positions):
            missing("Conditional discount requires all undiscounted position goods values")
            discount = Decimal(0)
        elif goods_total < D(approval["min_order_value"]):
            discount = Decimal(0)
    answer["discount_percent"] = discount
    trace(None, "order_discount", {"approval": approval, "undiscounted_goods": goods_total}, discount)
    pools = {}
    for pid, line in lines.items():
        key = key_for(pid, "setup_pool_membership")
        if key and not core_only:
            pools.setdefault(key, {})[pid] = line
    for key, members in pools.items():
        pool = payload("ruestpools").get("pools", {}).get(key)
        if not pool or any(v["material"].allocation_mass is None for v in members.values()):
            missing(f"Setup pool {key}: fee, allocation rule and allocation masses")
            continue
        if pool.get("allocation", "largest_remainder") != "largest_remainder" or pool.get("tie_break", "position_asc") not in ("position_asc", "position_desc"):
            missing(f"Setup pool {key}: unsupported allocation strategy needs clarification")
            continue
        allocated = largest_remainder(pool["fee"], {pid: v["qty"] * v["material"].allocation_mass for pid, v in members.items()}, pool.get("tie_break", "position_asc"))
        for pid, value in allocated.items():
            lines[pid]["shared_setup"] = value
        trace(None, "shared_setup", {"key": key, "pool": pool}, allocated)
    bonus = payload("projektbonus")
    eligible = {} if core_only else {pid: line["goods"] for pid, line in lines.items() if pid in bonus.get("positions", []) or key_for(pid, "bonus_membership")}
    if eligible and bonus.get("bands"):
        credit = marginal_credit(sum(eligible.values()), bonus["bands"])
        allocated = largest_remainder(credit, eligible)
        for pid, value in allocated.items():
            lines[pid]["bonus"] = value
        trace(None, "project_bonus", {"goods": eligible, "bands": bonus["bands"]}, allocated)
    for pid, line in lines.items():
        m, qty, base = line["material"], line["qty"], line["base"]
        if core_only:
            lead = m.lead_weeks_stock if m.stock_qty is not None and m.stock_qty >= pooled[m.number] else m.lead_weeks_production
            if lead is None or m.stock_qty is None:
                missing(f"Material {m.number}: stock and lead times for the core bid")
                continue
            price = cents(base * (100 - discount) / 100)
            result = {"position": pid, "material_number": m.number, "quantity": int(qty), "unit_price": price,
                      "lead_time_weeks": lead, "base_unit_price": base}
            answer["positions"].append(result)
            trace(pid, "core_price", {"base": base, "discount": discount, "stage": "core; commercial adjustments not yet applied"}, result)
            continue
        position_gaps = []
        setup = m.setup_fee
        if setup is None:
            position_gaps.append(f"Material {m.number}: ordinary setup fee (record zero only if stated absent)")
        mass = m.surcharge_mass
        surcharge = None
        if mass == 0:
            surcharge = Decimal(0)
        elif mass is not None:
            rate = m.surcharge_rate
            if m.surcharge_index:
                rates = payload("preisindizes").get("indices", {}).get(m.surcharge_index, [])
                valid = [r for r in rates if (not r.get("valid_from") or on_date(r["valid_from"]) <= scenario)
                         and (not r.get("valid_to") or scenario <= on_date(r["valid_to"]))]
                rate = D(valid[-1]["rate"]) if valid else None
            if rate is not None:
                formula = m.surcharge_formula or {"kind": "mass_times_rate"}
                adjusted = D(rate)
                if formula.get("kind") == "mass_times_excess":
                    adjusted -= D(formula["baseline"])
                if formula.get("floor") is not None:
                    adjusted = max(D(formula["floor"]), adjusted)
                surcharge = mass * adjusted * D(formula.get("multiplier", 1))
        if surcharge is None:
            position_gaps.append(f"Material {m.number}: surcharge mass, index code and scenario-valid index rate/formula (or evidenced zero)")
        tariff_key = key_for(pid, "packaging_tariff_key")
        if tariff_key:
            tariff = payload("verpackungstarife").get("tariffs", {}).get(tariff_key)
            if tariff is None or not m.carton_size:
                position_gaps.append(f"Position {pid}: packaging tariff {tariff_key} and carton size")
            else:
                cartons = (qty / m.carton_size).to_integral_value(rounding=ROUND_CEILING)
                line["carton_cost"] = cartons * D(tariff)
        group = key_for(pid, "condition_group")
        clause = payload("konditionsregeln").get("groups", {}).get(group) if group else "base_only"
        if clause not in ("base_only", "base_plus_surcharge", "none"):
            position_gaps.append(f"Position {pid}: commercial clause for condition group {group}")
        lead = None
        commitment_key = key_for(pid, "delivery_commitment_key")
        commitment = payload("terminfreigabe").get("commitments", {}).get(commitment_key)
        if commitment and commitment["material"] == m.number and pooled[m.number] <= D(commitment["ceiling_qty"]):
            lead = int(commitment["lead_weeks"])
        elif m.stock_qty is not None:
            lead = m.lead_weeks_stock if m.stock_qty >= pooled[m.number] else m.lead_weeks_production
        if lead is None:
            position_gaps.append(f"Material {m.number}: stock and stock/production lead times")
        if position_gaps:
            calc.gaps.extend(position_gaps)
            continue
        factor = (100 - discount) / 100
        final = base * (factor if clause != "none" else 1) + surcharge * (factor if clause == "base_plus_surcharge" else 1)
        final += (setup + line["shared_setup"] + line["carton_cost"] - line["bonus"]) / qty
        price = cents(final)
        reported_surcharge = surcharge + ((setup + line["shared_setup"]) / qty if include_setup_in_surcharge else 0)
        result = {"position": pid, "material_number": m.number, "quantity": int(qty), "unit_price": price,
                  "lead_time_weeks": lead, "base_unit_price": base, "surcharge_per_unit": cents(reported_surcharge),
                  "packaging_per_unit": cents(line["carton_cost"] / qty), "rebate_total": cents(line["bonus"])}
        answer["positions"].append(result)
        trace(pid, "final_unit_price", {"base": base, "surcharge": surcharge, "setup": setup,
            "shared_setup": line["shared_setup"], "carton_cost": line["carton_cost"], "bonus": line["bonus"],
            "quantity": qty, "clause": clause, "discount": discount}, result)
    answer["total_net"] = cents(sum((D(p["quantity"]) * p["unit_price"] for p in answer["positions"]), Decimal(0)))
    calc.complete = len(answer["positions"]) == len(resolved.positions) and not calc.gaps
    calc.gaps = list(dict.fromkeys(calc.gaps))
    return calc
