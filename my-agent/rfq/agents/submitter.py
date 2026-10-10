from __future__ import annotations

import json
from datetime import datetime, timezone

from ..rules.money import german


async def submit_frozen(agent, mailbox_agent, answer, context, until):
    ledger = agent.ledger
    async def fetch_tan():
        prior = len(ledger.of("TAN"))
        task = ("The submitter has just requested the signing TAN. Find the NEWEST signing message for this tender now. "
                "Use the current inbox, refresh/search as observed. Record a TAN with its value and any displayed request/expiry "
                "time. Ignore older codes. Do not request another TAN. Current time: " + datetime.now(timezone.utc).isoformat())
        await mailbox_agent.run(task, max_steps=15, until=until)
        records = ledger.of("TAN")
        if len(records) > prior:
            return "Fresh signing TAN: " + records[-1].value
        return "No fresh TAN found yet. Do not invent it; retry fetching or inspect the request status."
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
            "Do not navigate back after completing later wizard steps; fix in the current step when possible.\n"
            + "Frozen answer: " + json.dumps(answer, default=str, ensure_ascii=False)
            + "\nGerman formatted positions: " + json.dumps(display, ensure_ascii=False)
            + "\nGerman net total: " + german(answer["total_net"])
            + "\nRuntime context: " + json.dumps(context, default=str, ensure_ascii=False))
    agent.bus.emit("submit_step", "submitter", {"status": "starting"})
    result = await agent.run(task, max_steps=75, until=until, submit=True)
    agent.bus.emit("submit_step", "submitter", result)
    return result
