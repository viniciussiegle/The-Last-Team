from decimal import Decimal, ROUND_HALF_UP


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def cents(value) -> Decimal:
    return D(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_german(value: str) -> Decimal:
    cleaned = value.replace("\u00a0", "").replace(" ", "").replace("€", "").strip()
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    return D(cleaned)


def german(value) -> str:
    return f"{cents(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
