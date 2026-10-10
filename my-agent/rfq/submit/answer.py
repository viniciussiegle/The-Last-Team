import json
from decimal import Decimal

import jsonschema


def json_answer(answer):
    # JSON Schema requires numbers, while the calculation remains Decimal.
    return json.loads(json.dumps(answer, default=lambda v: float(v) if isinstance(v, Decimal) else str(v)))


def validate_answer(answer, schema):
    value = json_answer(answer)
    jsonschema.Draft202012Validator(schema).validate(value)
    return value
