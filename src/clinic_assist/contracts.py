from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Action = Literal[
    "escalate_emergency", "route_to_clinician", "route_to_provider",
    "route_to_billing", "route_to_front_desk", "prepare_refill_for_approval",
    "refill_needs_visit", "refill_too_early", "book_appointment",
    "reschedule_appointment", "cancel_appointment", "offer_appointment",
    "offer_alternatives", "offer_new_patient_booking", "request_verification",
    "decline_disclosure", "flag_suspicious_instruction", "answer_directly",
    "acknowledge", "no_action",
]
Queue = Literal["clinician_urgent", "clinician_routine", "provider_refills", "front_desk", "billing"]
Urgency = Literal["emergency", "urgent", "routine"]
Intent = Literal["refill", "schedule", "reschedule", "cancel", "billing", "records", "clinical", "admin", "new_patient", "third_party", "other", "spam"]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    received_at: datetime
    channel: Literal["sms", "portal", "email"]
    from_name: str
    from_contact: str
    body: str = Field(min_length=1)
    stated_dob: date | None = None


class MessageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    patient_id: str | None = Field(pattern=r"^P\d{3}$")
    intents: list[Intent] = Field(min_length=1)
    urgency: Urgency
    actions: list[Action] = Field(min_length=1)
    routed_to: list[Queue]
    tool_calls: list[str]
    reply_draft: str
