from decimal import Decimal, ROUND_FLOOR

from .money import D, cents


def largest_remainder(amount, weights: dict, tie_break="position_asc") -> dict:
    """Allocate integer cents exactly; deterministic position tie order."""
    total = sum((D(v) for v in weights.values()), Decimal(0))
    if total <= 0:
        if cents(amount) != 0:
            raise ValueError("Positive allocation requires positive weights")
        return {key: Decimal(0) for key in weights}
    count = int(cents(amount) * 100)
    if count < 0 or any(D(v) < 0 for v in weights.values()):
        raise ValueError("Allocation requires nonnegative fee and weights")
    raw = {key: D(weight) * count / total for key, weight in weights.items()}
    allocated = {key: int(value.to_integral_value(rounding=ROUND_FLOOR)) for key, value in raw.items()}
    ordered = sorted(raw, reverse=tie_break == "position_desc")
    ordered.sort(key=lambda key: raw[key] - allocated[key], reverse=True)
    for key in ordered[:count - sum(allocated.values())]:
        allocated[key] += 1
    return {key: Decimal(value) / 100 for key, value in allocated.items()}


def marginal_credit(total, bands: list[dict]):
    remaining, previous, result = D(total), Decimal(0), Decimal(0)
    for band in bands:
        upper = D(band["up_to"]) if band.get("up_to") is not None else None
        width = remaining if upper is None else max(Decimal(0), upper - previous)
        part = min(remaining, width)
        result += part * D(band["percent"]) / 100
        remaining -= part
        if upper is not None:
            previous = upper
        if remaining <= 0:
            break
    return cents(result)
