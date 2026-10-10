import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


def day(value) -> date:
    """A calendar day written as an ISO date or timestamp, or as day.month.year."""
    text = str(value).strip()
    dotted = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if dotted:
        return date(int(dotted.group(3)), int(dotted.group(2)), int(dotted.group(1)))
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"date '{text[:30]}' must be YYYY-MM-DD or DD.MM.YYYY") from None


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
