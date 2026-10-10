from __future__ import annotations

import json
import time
from decimal import Decimal

from ..evidence.resolver import normalized
from ..rules.money import german

MONEY = ("unit_price", "base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total")
# A form's session may end a minute or two after its last entry, so a signing code that takes longer is asked for anew.
TAN_FETCH_S = 90


def plain(value) -> str:
    """A number the way a choice list or a count field shows it: no trailing zeros, decimal comma."""
    return format(value.normalize(), "f").replace(".", ",") if isinstance(value, Decimal) else str(value)


async def submit_frozen(agent, mailbox_agent, answer, context, until):
    ledger = agent.ledger
    async def fetch_tan(request_info):
        ledger.tan_context = {"reference": context["reference"], "request_id": request_info.get("request_id")}
        prior = len(ledger.of("TAN"))
        task = ("The submitter has just requested a signing code for the tender below. Open the mailbox's inbox, find the "
                "newest signing-code message for this exact tender reference (a new request makes older codes invalid; "
                "refresh once if the newest message is not for this tender), read the code and record TAN with value and "
                "reference in the same call. The portal's request number, when given, names the message to use. Dates on "
                "these websites follow the scenario's clock: do not compare them with anything and do not verify "
                "timestamps. Do not request another code. The task ends as soon as the code is recorded. "
                "Signing context: " + json.dumps(ledger.tan_context, default=str))
        ledger.allow_tan = True
        try:
            await mailbox_agent.run(task, max_steps=12, until=min(until or float("inf"), time.monotonic() + TAN_FETCH_S), stop_record="TAN")
        finally:
            ledger.allow_tan = False
            ledger.tan_context = None
        records = ledger.of("TAN")
        if len(records) > prior:
            return "Signing TAN: " + records[-1].value
        return "No new signing code was found in time. Request a new code on the form and fetch again; do not invent a code."
    agent.tan_fetcher = fetch_tan
    # A form may ask for a product's attributes before it offers the product itself; the answer holds only its number.
    materials = {normalized(record.number): record for record in ledger.of("Material")}
    display = []
    for position in answer["positions"]:
        shown = {key: german(value) if key in MONEY else value for key, value in position.items()}
        material = materials.get(normalized(position["material_number"]))
        if material:
            shown["material_attributes"] = {"bore": plain(material.bore), "stroke": plain(material.stroke), "pressure": plain(material.pressure),
                                            "mounting": material.mounting, "certification": material.certification}
        display.append(shown)
    task = ("SUBMIT FROZEN ANSWER. Complete the website's bid/signature flow using only the frozen validated answer below. "
            "Follow the runtime brief's registration, bidder identity, document acknowledgements and submission requirements. "
            "Express interest/register if needed. Choose the decision specified.\n"
            "A form with several steps asks for one item per step. Read the step's heading first: it names the item asked "
            "for now, and items can come in any order. Enter only this answer's values for that item; an item that is not "
            "in the answer is left out with the form's own control for that, never filled from other information. Find "
            "out which control saves a step (forms usually say so near the step title) and finish every step with it in "
            "ONE browser call: actions=[...] ending with that control. A control that adds an item or row does not save; "
            "it creates one more empty entry. Where choices depend on each other and take effect through an apply "
            "control, use one fill per choice with the apply control as its submit_id, in the order shown, and put the "
            "fields that can be edited from the start into the first fill; a position's material_attributes are the "
            "values for such a product configuration. Write numbers the way the page writes them: the formatted values "
            "below use a decimal comma and two decimals for money; counts are whole numbers; a number the form asks for "
            "is entered even when it is zero. Enter net amounts, never gross/VAT: a total the form shows including tax "
            "is not the total to enter.\n"
            "After every call read the heading again before you act, also when a click was reported as failed: it may "
            "still have been carried out. If the heading changed, continue with the item now named and do not repeat the "
            "click. If it did not, the line near the step title says what the form refuses: correct only that. Never use "
            "a back control or the browser's back: it discards the steps already completed. Do not let the form wait: a "
            "session with no form entry for a minute or two may end, and the form then starts again at its first step; "
            "enter every step again from this answer.\n"
            "Request the signing TAN only on the review step, call fetch_signing_tan at once and enter the code as soon "
            "as you have it; if no code comes back, request a new one and fetch again. "
            "Before final confirmation compare the review summary's reference, decision, position material/quantity/price and total "
            "to this answer. Only then click the final submission control with confirm_final=true. Inspect the receipt and call done "
            "with submitted=true and receipt text only if accepted. Saving alone is not submission.\n"
            + "Frozen answer: " + json.dumps(answer, default=str, ensure_ascii=False)
            + "\nFormatted positions: " + json.dumps(display, ensure_ascii=False)
            + "\nFormatted net total: " + german(answer["total_net"])
            + "\nFormatted discount percent: " + plain(Decimal(str(answer["discount_percent"])))
            + "\nRuntime context: " + json.dumps(context, default=str, ensure_ascii=False))
    agent.bus.emit("submit_step", "submitter", {"status": "starting"})
    result = await agent.run(task, max_steps=75, until=until, submit=True)
    agent.bus.emit("submit_step", "submitter", result)
    return result
