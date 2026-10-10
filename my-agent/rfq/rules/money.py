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


MASS = {"kg": Decimal(1), "g": Decimal("0.001")}
LENGTH = {"mm": Decimal(1), "cm": Decimal(10), "m": Decimal(1000)}
PRESSURE = {"bar": Decimal(1)}
# Words that may stand next to a number without changing it.
BESIDE_A_NUMBER = {"eur", "€", "%", "st", "stk", "stück", "stueck", "pc", "pcs", "piece", "pieces", "je", "pro", "per",
                   "karton", "carton", "wochen", "woche", "weeks", "week"}


def number(value, units=None) -> Decimal:
    """A number as a page writes it, read by the format and unit the page states for it.

    '412,750 (en-US)' is 412750 and '1.905,30 (de-DE)' is 1905.30; a plain '1905.30' is taken as it stands. A
    lone comma before exactly three digits with no stated format could be either, so it is refused, not guessed.
    With units, a stated unit is converted to the first one listed ('4200 g' is 4.2 for a mass in kg); a unit
    that is not expected there is refused.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    text = str(value).replace("\u00a0", " ").strip()
    stated = re.search(r"\b(en|de)[-_ ]?([A-Za-z]{2})\b", text)
    plain = re.sub(r"\(.*?\)|\b(?:en|de)[-_ ]?[A-Za-z]{2}\b", " ", text)
    found = re.search(r"[-+]?\d[\d.,' ]*\d|[-+]?\d", plain)
    if not found:
        raise ValueError(f"no number in '{text[:40]}'")
    factor = Decimal(1)
    for word in re.findall(r"[^\W\d_]+|[€%]", plain[:found.start()] + " " + plain[found.end():]):
        word = word.casefold()
        if units and word in units:
            factor = units[word]
        elif word not in BESIDE_A_NUMBER:
            raise ValueError(f"'{text[:40]}': give only the number" + (f" in {next(iter(units))}" if units else "")
                             + " and the number format the page states; the unit '" + word + "' is not expected here")
    digits = found.group(0).replace(" ", "").replace("'", "")
    if stated and stated.group(1).lower() == "en":
        digits = digits.replace(",", "")
    elif stated and stated.group(1).lower() == "de":
        digits = digits.replace(".", "").replace(",", ".")
    elif "," in digits and "." in digits:
        # Whichever separator comes last is the decimal one.
        digits = digits.replace(".", "").replace(",", ".") if digits.rfind(",") > digits.rfind(".") else digits.replace(",", "")
    elif "," in digits:
        if re.fullmatch(r"[-+]?\d{1,3}(,\d{3}){2,}", digits):
            digits = digits.replace(",", "")  # several groups of three can only be thousands
        elif re.fullmatch(r"[-+]?[1-9]\d{0,2},\d{3}", digits):
            raise ValueError(f"'{text[:40]}' can be read two ways; copy the number format the page states next to it, "
                             "for example (en-US) or (de-DE)")
        else:
            digits = digits.replace(",", ".")
    try:
        return Decimal(digits) * factor
    except Exception:
        raise ValueError(f"'{text[:40]}' is not a number") from None


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else number(value)


def cents(value) -> Decimal:
    return D(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_german(value: str) -> Decimal:
    cleaned = value.replace("\u00a0", "").replace(" ", "").replace("€", "").strip()
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    return D(cleaned)


def german(value) -> str:
    return f"{cents(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
