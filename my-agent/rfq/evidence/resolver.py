from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr


def normalized(value):
    return " ".join(str(value).split()).casefold()


def timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamps require an explicit timezone")
    return parsed.astimezone(timezone.utc)


@dataclass
class Resolved:
    positions: list = field(default_factory=list)
    spec: object = None
    qa: list = field(default_factory=list)
    mails: dict = field(default_factory=dict)
    rejected: list = field(default_factory=list)
    gaps: list = field(default_factory=list)


def resolve(ledger, context):
    result = Resolved()
    reference = normalized(context["reference"])
    scenario = timestamp(context["scenario_date"])
    domain = context.get("bidder_domain")
    for info in ledger.of("TenderInfo"):
        if info.bidder_domain:
            domain = info.bidder_domain
    if not domain:
        result.gaps.append("Find the bidder's logged-in mailbox email/domain")
    # Duplicated observations preserve the latest complete record by identifier.
    result.positions = list({p.position_id: p for p in ledger.of("TenderPosition")}.values())
    specs = [s for s in ledger.of("SpecRevision") if s.status == "current"]
    if specs:
        ids = {s.revision_id for s in specs}
        if len(ids) > 1:
            result.gaps.append("Conflicting current specification revisions: verify status")
        else:
            result.spec = specs[-1]
            if hasattr(result.spec, "requirements"):
                # The same revision may be recorded incrementally from paged views.
                merged = {r.requirement_id: r for spec in specs for r in spec.requirements}
                result.spec = result.spec.model_copy(update={"requirements": list(merged.values())})
    if result.spec is None:
        result.gaps.append("Find the specification revision explicitly marked current")
    candidates = sorted((q for q in ledger.of("QAEntry") if timestamp(q.date) <= scenario), key=lambda q: timestamp(q.date))
    withdrawn = set()
    # A withdrawal can itself be withdrawn; evaluate the chain newest first.
    for q in reversed(candidates):
        if q.number not in withdrawn and q.kind == "withdrawal" and q.withdraws_number:
            withdrawn.add(q.withdraws_number)
    result.qa = [q for q in candidates if q.number not in withdrawn]
    for q in candidates:
        if q.number in withdrawn:
            result.rejected.append({"kind": "QAEntry", "id": q.number, "reason": "withdrawn"})
    for mail in ledger.of("MailRecord"):
        reason = None
        authorized = context.get("authorized_senders", {}).get(mail.kind)
        sender = parseaddr(mail.sender)[1].casefold()
        if normalized(mail.reference) != reference:
            reason = "other reference"
        elif timestamp(mail.date) > scenario:
            reason = "future-dated"
        elif not domain or not authorized or sender != f"{authorized}@{domain}".casefold():
            reason = "unauthorized sender"
        if reason:
            result.rejected.append({"kind": "MailRecord", "id": mail.message_id, "reason": reason})
            continue
        prior = result.mails.get(mail.kind)
        if prior is None or timestamp(mail.date) >= timestamp(prior.date):
            if prior:
                result.rejected.append({"kind": "MailRecord", "id": prior.message_id, "reason": "superseded"})
            result.mails[mail.kind] = mail
        else:
            result.rejected.append({"kind": "MailRecord", "id": mail.message_id, "reason": "superseded"})
    return result
