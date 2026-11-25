from decimal import Decimal

from langchain_core.tools import tool

from .domain import PAID_SCOPES, Decision, Offer, Proposal
from .sender import Sender, identify_sender, name_parts

MOVES = {
    "acceptance": "confirm_terms",
    "counteroffer": "open_quote",
    "clarification": "ask_clarification",
    "rejection": "decline",
}
OBLIGATION_LABELS = {
    "whitelisting": "whitelisting",
    "raw_assets": "raw assets",
    "additional_deliverables": "additional deliverables",
    "usage_restrictions": "usage restrictions",
    "contract_terms": "contract terms",
    "other": "other requirements in the proposal",
}


def scope(offer):
    return (
        offer.deliverables,
        offer.currency,
        offer.exclusive,
        offer.exclusive_days,
        offer.advertising,
        offer.usage_days,
        offer.usage_scope,
        offer.usage_included,
        offer.deadline_days,
    )


def plan_reply(decision: Decision, offer: Offer, sender: Sender, prior=None):
    changes = {
        "move": MOVES[decision.action],
        "sender_first_name": sender.first_name,
        "sender_evidence": sender.evidence,
        "deliverables": offer.deliverables or {},
        "exclusive": bool(offer.exclusive),
        "exclusive_days": offer.exclusive_days if offer.exclusive else None,
        "deadline_days": offer.deadline_days,
        "usage_days": offer.usage_days if offer.advertising else None,
        "usage_scope": offer.usage_scope
        if offer.advertising and offer.usage_scope in PAID_SCOPES
        else None,
    }
    if prior and decision.action == "counteroffer" and prior[0].total is not None:
        last_quote, last_offer = prior
        if scope(offer) != scope(last_offer):
            changes["move"] = "reprice_scope"
        elif offer.amount is not None and offer.amount < last_quote.total:
            changes["move"] = "hold_quote"
            if decision.total is not None and decision.total < last_quote.total:
                changes.update(
                    {key: getattr(last_quote, key) for key in ("items", "base", "surcharge", "total")}
                )
                changes["production_budget_separate"] = False
    return decision.model_copy(update=changes)


@tool
def select_negotiation_move(
    decision: Decision,
    offer: Offer,
    current_email: str,
    previous_decision: Decision | None = None,
    previous_offer: Offer | None = None,
) -> dict:
    """Ground the next negotiation move in checked terms, sender evidence and the last delivered quote."""
    prior = (previous_decision, previous_offer) if previous_decision and previous_offer else None
    return plan_reply(decision, offer, identify_sender(current_email), prior).model_dump()


def format_amount(currency, minor):
    return f"{currency} {Decimal(minor) / 100:,.2f}"


def delivery_label(deliverables):
    labels = []
    for item, quantity in deliverables.items():
        quantity_label = "one" if quantity == 1 else str(quantity)
        plural = "stories" if item == "story" else item + "s"
        labels.append(f"{quantity_label} {item if quantity == 1 else plural}")
    return join_labels(labels) if labels else "the deliverables"


def join_labels(labels):
    if len(labels) < 2:
        return labels[0] if labels else ""
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def next_question(decision):
    pending = set(decision.questions)
    if decision.unmapped_obligations:
        labels = dict.fromkeys(OBLIGATION_LABELS[item.kind] for item in decision.unmapped_obligations)
        return f"Could you clarify what you need for {join_labels(list(labels))}?"
    if "a shareable campaign brief" in pending:
        return "Could you send a shareable copy of the campaign brief?"
    if "the conflicting brief terms" in pending:
        return "Which version of the brief reflects the current terms?"
    if "deadline flexibility" in pending:
        return "Is there flexibility on the delivery date?"
    missing = []
    if pending & {"deliverables", "deliverables outside the rate card"}:
        missing.append("which deliverables you need")
    if "currency" in pending and "budget" in pending:
        missing.append("the budget and currency")
    elif "currency" in pending and not missing:
        return "Which currency is the budget in?"
    elif "currency" in pending:
        missing.append("the budget currency")
    elif "budget" in pending:
        missing.append("the available budget")
    if missing:
        return f"Could you confirm {join_labels(missing)}?"
    if "usage scope" in pending and "usage duration" in pending:
        return "Where will the paid ads run, and for how long?"
    if "usage scope" in pending:
        return "Where will the paid ads run?"
    if "usage duration" in pending:
        return "How long do you need to run the paid ads?"
    if "exclusivity duration" in pending:
        return "How long do you need category exclusivity?"
    if "separate paid usage budget" in pending:
        duration = f"{decision.usage_days}-day " if decision.usage_days else ""
        scope_label = decision.usage_scope or "paid advertising"
        return f"What budget have you set aside for the {duration}{scope_label} license?"
    return ""


def commercial_line(decision):
    label = delivery_label(decision.deliverables)
    if decision.exclusive or decision.exclusive_days or decision.surcharge:
        duration = f"{decision.exclusive_days} days of" if decision.exclusive_days else "category"
        label += f" and {duration} exclusivity"
        if "exclusivity duration" in decision.questions:
            label += " (duration to be agreed)"
    if decision.deadline_days is not None:
        label += f", with delivery within {decision.deadline_days} days"
    if decision.action == "acceptance" and decision.total is not None:
        amount = format_amount(decision.currency, decision.total)
        return f"{amount} works for {label}, subject to confirming the final brief."
    if decision.action == "counteroffer" and decision.total is not None:
        amount = format_amount(decision.currency, decision.total)
        if decision.move == "hold_quote":
            line = f"My quote for {label} remains {amount}."
        elif decision.production_budget_separate:
            line = f"{amount} works for {label} before licensing."
        elif decision.move == "reprice_scope":
            line = f"For the updated scope of {label}, my rate is {amount}."
        else:
            line = f"My rate for {label} is {amount}."
        if decision.advertising:
            line += " Paid ad usage is a separate license."
        elif len(decision.items) > 1:
            breakdown = join_labels(
                [
                    f"{format_amount(decision.currency, value)} for {item}"
                    for item, value in decision.items.items()
                ]
            )
            line += f" The base fee is {breakdown}."
        return line
    if decision.action == "rejection":
        if "Paid usage is outside the creator's policy." in decision.checks:
            return "I can't grant paid advertising rights for this campaign."
        if "Requested exclusivity exceeds policy." in decision.checks:
            return "I can't commit to the requested exclusivity window."
        return "I’ll pass on the proposed terms."
    return ""


def optional_step(decision):
    if decision.action == "acceptance":
        return "Could you send the final brief and proposed posting date?"
    if decision.action == "counteroffer" and decision.surcharge:
        return "If the budget is fixed, would a package without exclusivity work?"
    if decision.action == "counteroffer" and (
        len(decision.deliverables) > 1
        or any(quantity > 1 for quantity in decision.deliverables.values())
    ):
        return "If the budget is fixed, which deliverable would you keep?"
    if decision.action == "counteroffer":
        return "Is there room in the budget for that scope?"
    if decision.action == "rejection":
        if "Paid usage is outside the creator's policy." in decision.checks:
            return "Would an organic-only collaboration work?"
        if "Requested exclusivity exceeds policy." in decision.checks:
            return "Would a shorter exclusivity window work?"
        return ""
    return ""


def render_reply(decision: Decision, proposal: Proposal) -> str:
    candidate = decision.sender_first_name or ""
    name = candidate if len(name_parts(candidate)) == 1 else ""
    salutation = "Hello" if proposal.tone == "formal" else "Hi"
    greeting = f"{salutation} {name}," if name else f"{salutation},"
    step = next_question(decision)
    if not step and proposal.invite_followup:
        step = optional_step(decision)
    signoff = {"warm": "Best,", "concise": "Thanks,", "formal": "Regards,"}[proposal.tone]
    return "\n\n".join(
        filter(
            None,
            (
                greeting,
                commercial_line(decision),
                step,
                f"{signoff}\nCreator Partnerships",
            ),
        )
    )
