from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from ..rules.money import LENGTH, MASS, PRESSURE, day, number

# Numbers are copied from the page as written, with the page's format note and unit; code reads them.
Num = Annotated[Decimal, BeforeValidator(number)]
Mass = Annotated[Decimal, BeforeValidator(lambda value: number(value, MASS))]
Length = Annotated[Decimal, BeforeValidator(lambda value: number(value, LENGTH))]
Pressure = Annotated[Decimal, BeforeValidator(lambda value: number(value, PRESSURE))]
Weeks = Annotated[int, BeforeValidator(lambda value: value if isinstance(value, int) else int(number(value)))]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Provenance(StrictModel):
    site_id: str
    url: str
    title: str
    frame_path: str = "frame/0"
    snippet: str = Field(max_length=300)
    observed_at: datetime
    agent: str


class TenderInfo(StrictModel):
    reference: str
    title: str = ""
    buyer: str = ""
    deadline_raw: str = ""
    deadline_utc: datetime | None = None
    bidder_domain: str | None = None


class TenderPosition(StrictModel):
    position_id: str
    description: str
    quantity: Num = Field(gt=0)
    unit: Literal["piece", "set"]
    requirement_ref: str


class QAEntry(StrictModel):
    number: str
    date: datetime
    kind: Literal["quantity_change", "withdrawal", "technical_amendment", "condition_group", "packaging_tariff_key", "delivery_commitment_key", "setup_pool_membership", "bonus_membership", "other"]
    position_id: str | None = None
    payload: dict = Field(default_factory=dict)
    withdraws_number: str | None = None
    revision_ref: str | None = None
    requirement_ref: str | None = None


class Requirement(StrictModel):
    requirement_id: str
    katalog_group: str
    bore: Length
    stroke: Length
    stroke_kind: Literal["nominal", "usable"] = "nominal"
    pressure: Pressure
    mounting: str
    certification: str
    pieces_per_set: Num | None = None


class SpecRevision(StrictModel):
    revision_id: str
    status: Literal["current", "superseded", "unknown"]
    date: datetime | None = None
    requirements: list[Requirement]


class Material(StrictModel):
    number: str
    status: Literal["active", "blocked", "other"] | None = None
    katalog_group: str
    bore: Length
    stroke: Length
    pressure: Pressure
    mounting: str
    certification: str
    stock_qty: Num | None = None
    lead_weeks_stock: Weeks | None = None
    lead_weeks_production: Weeks | None = None
    carton_size: Num | None = None
    surcharge_mass: Mass | None = None
    allocation_mass: Mass | None = None
    setup_fee: Num | None = None
    surcharge_index: str | None = None
    surcharge_rate: Num | None = None
    surcharge_formula: dict | None = None


class PriceScale(StrictModel):
    min_qty: Num
    price: Num


class PriceCondition(StrictModel):
    material: str
    valid_from: str
    valid_to: str

    @field_validator("valid_from", "valid_to")
    @classmethod
    def as_iso_day(cls, value):
        return day(value).isoformat()

    currency: str
    fx_key: str | None = None
    price_unit_qty: Num = Field(gt=0)
    scales: list[PriceScale]


class MailRecord(StrictModel):
    message_id: str
    folder: str
    sender: str
    date: datetime
    subject: str
    reference: str
    kind: Literal["discount_approval", "konditionsregeln", "technikfreigabe", "verpackungstarife", "terminfreigabe", "devisenfixing", "ruestpools", "projektbonus", "preisindizes", "other"]
    payload: dict
    raw_text: str = ""
    confidence: Literal["high", "low"] = "high"


class SearchCoverage(StrictModel):
    katalog_group: str
    complete: bool
    candidate_materials: list[str]


class TAN(StrictModel):
    value: str
    reference: str | None = None
    request_id: str | None = None
    requested_at: datetime | None = None
    requested_at_precision: Literal["second", "minute"] = "second"
    expires_at: datetime | None = None


RECORD_TYPES = {cls.__name__: cls for cls in (TenderInfo, TenderPosition, QAEntry, SpecRevision, Material, PriceCondition, MailRecord, SearchCoverage, TAN)}


PAYLOAD_GUIDE = """
All dates need an explicit timezone. Copy every number exactly as the page writes
it, as a string, together with the number-format note the page gives for it (such
as a locale tag in brackets) and its unit: for example "412,750 (en-US)",
"1.905,30 (de-DE)", "840 g", "30 cm". Never convert, round or reformat a number
yourself; the same digits mean different amounts in different formats, and code
reads them. Lengths end up in mm, pressures in bar, masses in kg.
QA payloads: quantity_change {quantity}; technical_amendment {attributes: {field:value}};
condition_group/packaging_tariff_key/delivery_commitment_key/setup_pool_membership/
bonus_membership {key}. For multiple positions, record one QAEntry per position.
MailRecord payloads (record candidate mails even when unauthorized or superseded):
discount_approval {percent, min_order_value?};
konditionsregeln {groups: {key: 'base_only'|'base_plus_surcharge'|'none'}};
technikfreigabe {reserves: [{revision, requirement, per_end}]};
verpackungstarife {tariffs: {key: decimal_eur_per_carton}};
terminfreigabe {commitments: {key: {material, lead_weeks, ceiling_qty}}};
devisenfixing {rates: {key: decimal_eur_per_currency_unit}};
ruestpools {pools: {key: {fee, tie_break: 'position_asc'|'position_desc', allocation: 'largest_remainder'}}};
projektbonus {positions: [position_id], bands: [{up_to: decimal|null, percent: decimal}]};
preisindizes {indices: {code: [{valid_from: date|null, valid_to: date|null, rate: decimal}]} }.
Material surcharge_formula can specify {kind:'mass_times_rate'|'mass_times_excess',
baseline: decimal, multiplier: decimal}. Record the exact formula's parameters;
do not silently infer an excess formula or floor unless text states it. Optional
{floor: decimal} floors the per-kg rate difference. Material surcharge_rate is
only for a directly stated fixed rate; otherwise record surcharge_index.
Material status: give it only after reading the material's own status field and, where that is a code, the
meaning the site states for the code; until then leave status out. A page header or a guess is not a status.
Record every candidate of a group, not only the first: look-alikes differ in status or one attribute.
Set known absent setup_fee/surcharge_mass to decimal zero only when evidenced.
SearchCoverage: complete=true only after inspecting all candidates in the
model-selected catalogue group, including paging if present. Needed to prove
no matching active material. Do not infer no_bid from an unfinished search.
TAN values are ephemeral and should be recorded only when signing is requested.
"""
