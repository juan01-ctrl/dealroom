from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Deliverable = Literal["reel", "story", "post", "video"]
Money = Annotated[int, Field(ge=0, le=1_000_000_000)]
Quantity = Annotated[int, Field(ge=1, le=100)]
Days = Annotated[int, Field(ge=0, le=3650)]
Currency = Literal["USD", "EUR", "GBP", "CAD", "AUD"]
ObligationKind = Literal[
    "whitelisting",
    "raw_assets",
    "additional_deliverables",
    "usage_restrictions",
    "contract_terms",
    "other",
]
PAID_SCOPES = {"paid social", "paid search", "display ads"}
Action = Literal["acceptance", "counteroffer", "clarification", "rejection"]
Move = Literal[
    "open_quote", "hold_quote", "reprice_scope", "confirm_terms", "ask_clarification", "decline"
]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UnmappedObligation(Input):
    kind: ObligationKind
    status: Literal["unsupported", "ambiguous"]
    detail: str = Field(min_length=1, max_length=180)


class Offer(Input):
    brand: str | None = Field(default=None, max_length=120)
    deliverables: dict[Deliverable, Quantity] | None = None
    amount: Money | None = None
    currency: Currency | None = None
    deadline_days: Days | None = None
    exclusive: bool | None = None
    exclusive_days: Days | None = None
    advertising: bool | None = None
    usage_scope: str | None = Field(default=None, max_length=120)
    usage_days: Days | None = None
    usage_included: bool | None = None
    unmapped_obligations: list[UnmappedObligation] = Field(default_factory=list)


class OfferPatch(Offer):
    remove_deliverables: list[Deliverable] = Field(default_factory=list, max_length=4)
    replace_deliverables: bool = False
    unmapped_obligations: list[UnmappedObligation] = Field(default_factory=list, max_length=20)
    remove_unmapped_obligations: list[UnmappedObligation] = Field(default_factory=list, max_length=20)


class Policy(Input):
    currency: Currency = "USD"
    rates: dict[Deliverable, Money] = Field(
        default_factory=lambda: {"reel": 200000, "story": 50000, "post": 100000, "video": 300000}
    )
    minimum_reel: Money = 150000
    exclusivity_pct: int = Field(default=30, ge=0, le=300)
    lead_days: Days = 7
    max_exclusive_days: Days = 90
    allow_paid_usage: bool = True
    tone: Literal["warm", "concise", "formal"] = "warm"

    @model_validator(mode="after")
    def valid_floor(self):
        if "reel" not in self.rates or not 0 < self.minimum_reel <= self.rates["reel"]:
            raise ValueError("The reel floor must be positive and at or below the standard rate.")
        if any(rate == 0 for rate in self.rates.values()):
            raise ValueError("Standard rates must be positive.")
        return self


class Decision(Input):
    action: Action
    currency: Currency | None = None
    items: dict[Deliverable, int] = Field(default_factory=dict)
    base: int | None = None
    surcharge: int | None = None
    total: int | None = None
    questions: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    advertising: bool = False
    move: Move | None = None
    sender_first_name: str | None = None
    sender_evidence: Literal["mailbox", "signature", "ambiguous", "unverified"] = "unverified"
    deliverables: dict[Deliverable, int] = Field(default_factory=dict)
    exclusive: bool = False
    exclusive_days: Days | None = None
    deadline_days: Days | None = None
    usage_days: Days | None = None
    usage_scope: Literal["paid social", "paid search", "display ads"] | None = None
    production_budget_separate: bool = False
    unmapped_obligations: list[UnmappedObligation] = Field(default_factory=list)


class Proposal(Input):
    tone: Literal["warm", "concise", "formal"] = "warm"
    invite_followup: bool = True


def merge(previous: Offer, patch: Offer) -> Offer:
    fields = patch.model_dump(exclude_unset=True)
    removed = fields.pop("remove_deliverables", [])
    replace = fields.pop("replace_deliverables", False)
    fields.pop("remove_unmapped_obligations", None)
    if isinstance(patch, OfferPatch):
        obligations = [
            item
            for item in previous.unmapped_obligations
            if item not in patch.remove_unmapped_obligations
        ]
        for item in patch.unmapped_obligations:
            if item not in obligations:
                obligations.append(item)
        fields["unmapped_obligations"] = [item.model_dump() for item in obligations]
    if fields.get("deliverables") is not None or removed:
        items = {} if replace else dict(previous.deliverables or {})
        items.update(fields.get("deliverables") or {})
        for item in removed:
            items.pop(item, None)
        fields["deliverables"] = items
    if "exclusive" in fields and fields["exclusive"] is not True:
        fields["exclusive_days"] = None
    if "advertising" in fields and fields["advertising"] is not True:
        fields.update(usage_scope=None, usage_days=None, usage_included=None)
    return Offer.model_validate(previous.model_dump() | fields)


def surcharge(base: int, percent: int) -> int:
    return int((Decimal(base) * percent / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def decide(offer: Offer, policy: Policy) -> Decision:
    questions = []
    if not offer.deliverables:
        questions.append("deliverables")
    elif offer.deliverables.keys() - policy.rates.keys():
        questions.append("deliverables outside the rate card")
    if offer.currency != policy.currency:
        questions.append("currency")
    if offer.amount is None:
        questions.append("budget")
    if offer.exclusive and not offer.exclusive_days:
        questions.append("exclusivity duration")
    if offer.advertising:
        if offer.usage_scope not in PAID_SCOPES:
            questions.append("usage scope")
        if not offer.usage_days:
            questions.append("usage duration")
        questions.append("separate paid usage budget")
    if offer.advertising and not policy.allow_paid_usage:
        return Decision(action="rejection", checks=["Paid usage is outside the creator's policy."])
    if offer.exclusive and offer.exclusive_days and offer.exclusive_days > policy.max_exclusive_days:
        return Decision(action="rejection", checks=["Requested exclusivity exceeds policy."])
    if offer.unmapped_obligations:
        return Decision(
            action="clarification",
            questions=["unmapped obligations", *questions],
            checks=["Unmapped obligations require review before quoting."],
            unmapped_obligations=offer.unmapped_obligations,
        )
    if offer.deadline_days is not None and offer.deadline_days < policy.lead_days:
        return Decision(
            action="clarification",
            questions=questions + ["deadline flexibility"],
            checks=["Requested deadline conflicts with creator availability."],
        )
    if (
        not offer.deliverables
        or offer.deliverables.keys() - policy.rates.keys()
        or (offer.currency != policy.currency or offer.amount is None)
    ):
        return Decision(
            action="clarification",
            questions=questions,
            checks=["Confirm required terms."],
            advertising=bool(offer.advertising),
        )
    items = {item: policy.rates[item] * quantity for item, quantity in offer.deliverables.items()}
    base = sum(items.values())
    extra = surcharge(base, policy.exclusivity_pct) if offer.exclusive else 0
    floor = sum(
        (policy.minimum_reel if item == "reel" else policy.rates[item]) * quantity
        for item, quantity in offer.deliverables.items()
    )
    minimum = floor + (surcharge(floor, policy.exclusivity_pct) if offer.exclusive else 0)
    checks = [
        "Offer below commercial constraints." if offer.amount < minimum else "Base floor satisfied."
    ]
    if offer.exclusive:
        checks += ["Exclusivity surcharge applied."]
    if offer.advertising:
        checks += ["Paid usage handled separately."]
    if offer.amount < base + extra or offer.advertising:
        action = "counteroffer"
    elif questions:
        action = "clarification"
    else:
        action = "acceptance"
    separate_budget = (
        offer.advertising and offer.usage_included is False and offer.amount >= base + extra
    )
    if action == "acceptance" or separate_budget:
        # Recover the base from a total that includes any applicable exclusivity.
        target_base = base
        percent = policy.exclusivity_pct if offer.exclusive else 0
        base = (offer.amount * 100 + 49) // (100 + percent)
        extra = offer.amount - base
        allocation = {item: amount * base // target_base for item, amount in items.items()}
        largest = max(items, key=items.__getitem__)
        allocation[largest] += base - sum(allocation.values())
        items = allocation
    return Decision(
        action=action,
        currency=offer.currency,
        items=items,
        base=base,
        surcharge=extra,
        total=base + extra,
        questions=questions,
        checks=checks,
        advertising=bool(offer.advertising),
        production_budget_separate=bool(separate_budget),
    )
