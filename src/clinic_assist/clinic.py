"""Policy checks and state changes. No LLM or evaluation labels live here."""
import copy
from datetime import date, datetime, time, timedelta
from threading import RLock
from uuid import uuid4
from typing import get_args

from .contracts import Action, Intent, Message, Queue, ResponseLanguage, Urgency
from .data import ClinicData


def norm(value: str) -> str:
    return " ".join(value.casefold().split())


def minutes(value: str) -> int:
    value = time.fromisoformat(value)
    return value.hour * 60 + value.minute


def clock(value: int) -> str:
    return f"{value // 60:02}:{value % 60:02}"


class Clinic:
    def __init__(self, data: ClinicData):
        self.data = data.clone()
        self.routes: list[dict] = []
        self.refill_requests: list[dict] = []
        self.events: list[dict] = []
        self.message: Message | None = None
        self.urgency: Urgency | None = None
        self.intents: list[str] = []
        self.response_language: ResponseLanguage = "unspecified"
        self._lock = RLock()

    def begin_message(self, message: dict) -> dict:
        self.message = Message.model_validate(message)
        self.urgency = None
        self.intents = []
        self.response_language = "unspecified"
        return {"id": self.message.id, "clinic_date": self.data.today.isoformat()}

    def _context(self) -> Message:
        if self.message is None:
            raise ValueError("Host must bind a message first")
        return self.message

    def _record(self, kind: str, **details) -> dict:
        event = {"message_id": self._context().id, "kind": kind, **details}
        self.events.append(copy.deepcopy(event))
        return event

    def triage(self, urgency: Urgency, intents: list[Intent], response_language: ResponseLanguage = "unspecified") -> dict:
        self._context()
        if urgency not in {"routine", "urgent", "emergency"} or not intents:
            raise ValueError("Invalid urgency or empty intents")
        if any(intent not in get_args(Intent) for intent in intents):
            raise ValueError("Use canonical intent names from the triage tool schema")
        if response_language not in get_args(ResponseLanguage):
            raise ValueError("Unknown response language")
        if self.urgency == "emergency" and urgency != "emergency":
            raise ValueError("Emergency triage cannot be downgraded")
        self.urgency, self.intents = urgency, intents
        if response_language != "unspecified":
            self.response_language = response_language
        if urgency == "emergency":
            self.route("clinician_urgent", "Possible medical emergency; immediate review required")
        return {"urgency": urgency, "intents": intents, "response_language": self.reply_language()}

    def reply_language(self) -> str:
        if self.response_language != "unspecified":
            return self.response_language
        return self.find_patient().get("preferred_language", "English")

    def _verified(self) -> dict:
        m = self._context()
        candidates = []
        for p in self.data.patients:
            contact = norm(m.from_contact)
            known = [p.get("phone", ""), p.get("email", "")]
            caregiver = p.get("authorised_caregiver", {})
            caregiver_contact = [caregiver.get("phone", ""), caregiver.get("email", "")]
            contact_match = bool(contact) and contact in {norm(x) for x in known if x}
            caregiver_match = bool(contact) and contact in {norm(x) for x in caregiver_contact if x}
            name_dob_match = (norm(m.from_name) == norm(p["name"])
                              and m.stated_dob is not None and m.stated_dob.isoformat() == p["dob"])
            if contact_match or caregiver_match or name_dob_match:
                candidates.append(p)
        if len(candidates) != 1:
            raise PermissionError("Sender is not uniquely verified; request verification without disclosing records")
        return candidates[0]

    def find_patient(self) -> dict:
        try:
            p = self._verified()
        except PermissionError:
            return {"verified": False, "patient_id": None}
        return {"verified": True, "patient_id": p["id"], "name": p["name"],
                "primary_provider": p["primary_provider"], "preferred_language": p["preferred_language"]}

    def list_medications(self) -> list[dict]:
        patient_id = self._verified()["id"]
        return copy.deepcopy([m for m in self.data.medications if m["patient_id"] == patient_id])

    def _medication(self, medication: str) -> dict:
        aliases = {"xanax": "alprazolam", "metformina": "metformin"}
        query = aliases.get(norm(medication), norm(medication))
        meds = [m for m in self.list_medications() if query and query in norm(m["medication"])]
        if len(meds) != 1:
            raise ValueError("Medication must uniquely match the verified patient's active list")
        return meds[0]

    def check_refill(self, medication: str) -> dict:
        p = self._verified()
        m = self._medication(medication)
        today = self.data.today
        try:
            cutoff = today.replace(year=today.year - 1)
        except ValueError:
            cutoff = today.replace(year=today.year - 1, day=28)
        earliest = date.fromisoformat(m["last_filled"]) + timedelta(days=m["days_supply"] - 7)
        if m["controlled_schedule"]:
            status = "controlled"
        elif date.fromisoformat(p["last_visit"]) < cutoff:
            status = "needs_visit"
        elif m["refills_remaining"] == 0:
            status = "no_refills"
        elif today < earliest:
            status = "too_early"
        else:
            status = "eligible"
        decision = {"status": status, "medication": m["medication"], "prescriber": m["prescriber"],
                    "earliest_request_date": earliest.isoformat(), "approval_required": True}
        self._record("refill_check", decision=decision)
        return decision

    def _action_allowed(self) -> None:
        if self.urgency is None:
            raise PermissionError("Triage the message before performing an action")
        if self.urgency == "emergency":
            raise PermissionError("Emergency message: escalation only, no administrative mutations")
        self._verified()

    def prepare_refill(self, medication: str) -> dict:
        with self._lock:
            self._action_allowed()
            decision = self.check_refill(medication)
            if decision["status"] != "eligible":
                raise ValueError("Refill cannot be prepared: " + decision["status"])
            patient_id = self._verified()["id"]
            for request in self.refill_requests:
                if request["patient_id"] == patient_id and request["medication"] == decision["medication"]:
                    self.route("provider_refills", "Existing refill request awaits provider approval")
                    self._record("prepare_refill_for_approval", request_id=request["id"], existing=True)
                    return copy.deepcopy(request)
            request = {"id": "R" + uuid4().hex[:12], "patient_id": patient_id,
                       "medication": decision["medication"], "prescriber": decision["prescriber"],
                       "status": "pending_provider_approval"}
            self.refill_requests.append(request)
            self.route("provider_refills", "Refill prepared for provider approval")
            self._record("prepare_refill_for_approval", request_id=request["id"])
            return copy.deepcopy(request)

    def list_appointments(self) -> list[dict]:
        patient_id = self._verified()["id"]
        return copy.deepcopy([a for a in self.data.appointments if a["patient_id"] == patient_id
                              and a["status"] == "booked" and a["date"] >= self.data.today.isoformat()])

    def _slot(self, provider: str, day: str, start: str, visit_type: str, ignore: str | None = None) -> dict:
        c = self.data.clinic
        if visit_type not in c["visit_types"]:
            raise ValueError("Unknown visit type")
        person = next((p for p in c["providers"] if p["id"] == provider), None)
        if person is None:
            raise ValueError("Unknown provider")
        d = date.fromisoformat(day)
        begin = minutes(start)
        end = begin + c["visit_types"][visit_type]
        if d < self.data.today or d.strftime("%a") not in c["hours"]["days"] or day in c["holidays"]:
            raise ValueError("Clinic is closed or date is in the past")
        if any(lo <= day <= hi for lo, hi in person.get("leave", [])):
            raise ValueError("Provider is on leave")
        opening, closing = minutes(c["hours"]["open"]), minutes(c["hours"]["close"])
        lunch_start, lunch_end = map(minutes, c["hours"]["lunch"])
        if begin % 20 or begin < opening or end > closing or (begin < lunch_end and end > lunch_start):
            raise ValueError("Invalid start, duration or lunch overlap")
        m = self._context()
        local = m.received_at.astimezone(self.data.timezone)
        if d == self.data.today and begin < local.hour * 60 + local.minute:
            raise ValueError("Appointment is before the message was received")
        for a in self.data.appointments:
            if (a["id"] != ignore and a["status"] == "booked" and a["provider"] == provider and a["date"] == day
                    and begin < minutes(a["end"]) and end > minutes(a["start"])):
                raise ValueError("Appointment would overlap an existing booking")
        return {"provider": provider, "date": day, "start": clock(begin), "end": clock(end), "visit_type": visit_type}

    def find_slots(self, provider: str, start_date: str, end_date: str, visit_type: str,
                   time_from: str = "09:00", time_until: str = "17:00", limit: int = 8) -> list[dict]:
        self._context()
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if end < start or (end - start).days > 60 or not 1 <= limit <= 30:
            raise ValueError("Search must cover 0-60 days with limit 1-30")
        if provider not in {p["id"] for p in self.data.clinic["providers"]} or visit_type not in self.data.clinic["visit_types"]:
            raise ValueError("Unknown provider or visit type")
        slots = []
        for offset in range((end - start).days + 1):
            day = (start + timedelta(days=offset)).isoformat()
            for begin in range(9 * 60, 17 * 60, 20):
                try:
                    slot = self._slot(provider, day, clock(begin), visit_type)
                except ValueError:
                    continue
                if begin >= minutes(time_from) and minutes(slot["end"]) <= minutes(time_until):
                    slots.append(slot)
                    if len(slots) == limit:
                        self._record("slot_search", slots=slots)
                        return slots
        self._record("slot_search", slots=slots)
        return slots

    def book(self, provider: str, day: str, start: str, visit_type: str) -> dict:
        with self._lock:
            self._action_allowed()
            slot = self._slot(provider, day, start, visit_type)
            appt = {"id": "B" + uuid4().hex[:12], "patient_id": self._verified()["id"], **slot, "status": "booked"}
            self.data.appointments.append(appt)
            self._record("book_appointment", appointment=appt)
            return copy.deepcopy(appt)

    def _owned(self, appointment_id: str) -> dict:
        p = self._verified()
        a = next((a for a in self.data.appointments if a["id"] == appointment_id
                  and a["patient_id"] == p["id"] and a["status"] == "booked"), None)
        if a is None:
            raise PermissionError("No such booked appointment for the verified patient")
        return a

    def cancel(self, appointment_id: str) -> dict:
        with self._lock:
            self._action_allowed()
            a = self._owned(appointment_id)
            a["status"] = "cancelled"
            self._record("cancel_appointment", appointment=copy.deepcopy(a))
            return copy.deepcopy(a)

    def reschedule(self, appointment_id: str, provider: str, day: str, start: str) -> dict:
        with self._lock:
            self._action_allowed()
            a = self._owned(appointment_id)
            slot = self._slot(provider, day, start, a["visit_type"], ignore=appointment_id)
            a.update(slot)
            self._record("reschedule_appointment", appointment=copy.deepcopy(a))
            return copy.deepcopy(a)

    def route(self, queue: Queue, reason: str) -> dict:
        if queue not in self.data.clinic["routing_queues"]:
            raise ValueError("Unknown queue")
        item = {"message_id": self._context().id, "queue": queue, "reason": reason,
                "patient_id": self.find_patient()["patient_id"]}
        if not any(x["message_id"] == item["message_id"] and x["queue"] == queue for x in self.routes):
            self.routes.append(item)
            self._record("route", queue=queue)
        return copy.deepcopy(item)

    def draft_reply(self, text: str, language: str = "English") -> dict:
        if self.urgency == "emergency":
            text = ("Llame al 911 o vaya al servicio de urgencias más cercano. Se ha alertado a la clínica."
                    if norm(language) == "spanish" else
                    "Call 911 or go to the nearest emergency department. The clinic has been alerted.")
        self._record("draft_reply", reply_draft=text)
        return {"reply_draft": text, "sent": False}

    def audit(self) -> dict:
        id = self._context().id
        return {"patient_id": self.find_patient()["patient_id"], "urgency": self.urgency, "intents": self.intents,
                "response_language": self.reply_language(),
                "routes": copy.deepcopy([r for r in self.routes if r["message_id"] == id]),
                "events": copy.deepcopy([e for e in self.events if e["message_id"] == id])}

    def draft_actions(self, actions: list[str], language: str = "English") -> dict:
        from .replies import render_reply
        audit = self.audit()
        executed = {e["kind"] for e in audit["events"]}
        if not actions or any(action not in get_args(Action) for action in actions):
            raise ValueError("Use nonempty canonical actions")
        if "escalate_emergency" in actions and self.urgency != "emergency":
            raise ValueError("No emergency triage supports this action")
        if "refill_needs_visit" in actions and not any(e["kind"] == "refill_check" and
                e["decision"]["status"] == "needs_visit" for e in audit["events"]):
            raise ValueError("No needs_visit refill check supports this reply")
        mutations = {"prepare_refill_for_approval", "book_appointment", "reschedule_appointment", "cancel_appointment"}
        if (set(actions) & mutations) - executed:
            raise ValueError("Cannot draft an unperformed mutation")
        queues = {r["queue"] for r in audit["routes"]}
        for action, required in {"route_to_billing": {"billing"}, "route_to_front_desk": {"front_desk"},
                "route_to_provider": {"provider_refills"}, "route_to_clinician": {"clinician_urgent", "clinician_routine"}}.items():
            if action in actions and not queues.intersection(required):
                raise ValueError("Cannot draft an unperformed handoff")
        return self.draft_reply(render_reply(actions, self.data.clinic, audit, language), language)

    def finish_message(self, actions: list[Action]) -> dict:
        if self.urgency is None:
            raise ValueError("Triage is required before completion")
        mutations = {"prepare_refill_for_approval", "book_appointment", "reschedule_appointment", "cancel_appointment"}
        performed = {e["kind"] for e in self.audit()["events"]} & mutations
        actions = sorted(set(actions) | performed)
        if self.urgency == "emergency" and (set(actions) - performed) != {"escalate_emergency"}:
            raise ValueError("Emergency completion may only escalate and report already-performed operations")
        return {"actions": actions, "safety_error": "Emergency reclassification after administrative mutation" if self.urgency == "emergency" and performed else None,
                **self.draft_actions(actions, self.reply_language())}
