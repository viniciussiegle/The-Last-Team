from __future__ import annotations

import json

from ..rules.money import german


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
            await mailbox_agent.run(task, max_steps=12, until=until, stop_record="TAN")
        finally:
            ledger.allow_tan = False
            ledger.tan_context = None
        records = ledger.of("TAN")
        if records:
            return ("Signing TAN: " if len(records) > prior else "No newer code was recorded; the last one read was: ") + records[-1].value
        return "No signing code was found. Check the portal's request status and fetch again; do not invent a code."
    agent.tan_fetcher = fetch_tan
    display = []
    for position in answer["positions"]:
        display.append({key: german(value) if key in ("unit_price", "base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total") else value
                        for key, value in position.items()})
    task = ("SUBMIT FROZEN ANSWER. Complete the website's bid/signature flow using only the frozen validated answer below. "
            "Follow the runtime brief's registration, bidder identity, document acknowledgements and submission requirements. "
            "Express interest/register if needed. Choose the decision specified; complete every computed position and explicitly "
            "omit any unfinished tender positions if the form permits it. Save frequently. Enter net amounts, never gross/VAT. "
            "Navigate using observed controls. Request the signing TAN only when ready to sign, then use fetch_signing_tan. "
            "Before final confirmation compare the review summary's reference, decision, position material/quantity/price and total "
            "to this answer. Only then click the final submission control with confirm_final=true. Inspect the receipt and call done "
            "with submitted=true and receipt text only if accepted. Saving alone is not submission. "
            "Enter only what the frozen answer contains: a position that is not in it is omitted with the form's own "
            "omit control, never filled from other information. Never use a back control or the browser's back: it "
            "discards the steps already completed. Always move forward; an entry you doubt is checked on the review step.\n"
            + "Frozen answer: " + json.dumps(answer, default=str, ensure_ascii=False)
            + "\nGerman formatted positions: " + json.dumps(display, ensure_ascii=False)
            + "\nGerman net total: " + german(answer["total_net"])
            + "\nRuntime context: " + json.dumps(context, default=str, ensure_ascii=False))
    agent.bus.emit("submit_step", "submitter", {"status": "starting"})
    # An expiring session discards the unsaved form, so the one flow that must not be interrupted starts on a
    # new session instead of the one opened when gathering began.
    try:
        await agent.session.context.clear_cookies()
        await agent.session.page.goto(agent.session.site["url"], wait_until="domcontentloaded", timeout=15000)
    except Exception:
        pass
    result = await agent.run(task, max_steps=75, until=until, submit=True)
    agent.bus.emit("submit_step", "submitter", result)
    return result
