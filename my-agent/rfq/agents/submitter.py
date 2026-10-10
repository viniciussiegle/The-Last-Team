from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from ..rules.money import german


async def submit_frozen(agent, mailbox_agent, answer, context, until):
    ledger = agent.ledger
    flow_started = datetime.now(timezone.utc)
    async def fetch_tan(request_info):
        requested_after = flow_started
        if request_info.get("requested_at"):
            from ..evidence.resolver import timestamp
            try:
                requested_after = timestamp(request_info["requested_at"])
            except ValueError:
                return "Re-check the displayed portal request time and its timezone."
            display_interval = timedelta(seconds=59 if request_info.get("requested_at_precision") == "minute" else 0)
            if requested_after + display_interval < flow_started - timedelta(seconds=5) or requested_after > datetime.now(timezone.utc) + timedelta(seconds=5):
                return "Portal request time does not fit the current signing flow; re-check the current request status."
            requested_after = max(requested_after, flow_started)
        ledger.tan_context = {"reference": context["reference"], "requested_after": requested_after,
                              "request_id": request_info.get("request_id")}
        prior = len(ledger.of("TAN"))
        task = ("The submitter has requested the signing TAN. Find the current signing message for the exact tender below. "
                "Use the current inbox, refresh/search as observed. Record TAN with value, reference and displayed request_id, "
                "requested_at and expires_at including timezone. Set requested_at_precision=minute only if the displayed request "
                "time omits seconds. Ignore older or expired codes. Do not assume an unseen newer "
                "request exists, and do not request another TAN. The task ends immediately after a valid current code is recorded. "
                "Signing context: " + json.dumps(ledger.tan_context, default=str) + " Current time: " + datetime.now(timezone.utc).isoformat())
        ledger.allow_tan = True
        try:
            await mailbox_agent.run(task, max_steps=15, until=until, stop_record="TAN")
        finally:
            ledger.allow_tan = False
            ledger.tan_context = None
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
