"""Policy checks and state changes. No LLM or evaluation labels live here."""
import copy
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from threading import RLock
from uuid import uuid4
from typing import get_args

from .contracts import Action, Intent, Message, Queue, ResponseLanguage, Urgency
from .appointment_requests import validate_requested_slot, validate_mutation_consent, requested_visit_types
from .data import ClinicData, calendar_context


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

    def public_policy(self) -> dict:
        policy = copy.deepcopy(self.data.clinic)
        policy['date_context'] = calendar_context(self.data.today)
        return policy

    def begin_message(self, message: dict) -> dict:
        self.message = Message.model_validate(message)
        self.urgency = None
        self.intents = []
        self.response_language = "unspecified"
        self._unavailable_requests: list[dict] = []
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
        elif urgency == 'urgent' and 'clinical' in intents:
            self.route('clinician_urgent', 'Urgent clinical concern; same-day clinical review required')
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
        appointments = copy.deepcopy([a for a in self.data.appointments if a["patient_id"] == patient_id
                              and a["status"] == "booked" and a["date"] >= self.data.today.isoformat()])
        for appointment in appointments:
            following = calendar_context(date.fromisoformat(appointment['date']))['next_week']
            appointment['following_week'] = {key: following[key] for key in ('start_date', 'end_date')}
        return appointments

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

    def search_slots(self, provider: str, start_date: str, end_date: str, visit_type: str,
                     time_from: str = '09:00', time_until: str = '17:00', limit: int = 8) -> dict:
        """Keep requested results separate from bounded, genuinely available alternatives."""
        slots=self.find_slots(provider,start_date,end_date,visit_type,time_from,time_until,limit)
        alternatives=[]
        if not slots:
            if self._search_covers_request(provider,start_date,end_date,visit_type,time_from,time_until):
                self._unavailable_requests.append(dict(provider=provider,start_date=start_date,end_date=end_date,
                    visit_type=visit_type,time_from=time_from,time_until=time_until))
            after=date.fromisoformat(end_date)+timedelta(days=1)
            alternatives=self.find_slots(provider,after.isoformat(),(after+timedelta(days=27)).isoformat(),
                visit_type,time_from,time_until,min(limit,3))
        return {'slots':slots,'alternatives':alternatives,
                'note':'Alternative dates require sender confirmation; do not silently substitute them.' if alternatives else
                    ('No slots found within the requested range or following 28 days; route to front_desk, do not claim an offer.' if not slots else 'Requested-range slots available.')}

    def _search_covers_request(self, provider: str, start_date: str, end_date: str,
                               visit_type: str, time_from: str, time_until: str) -> bool:
        """Only a complete source-grounded query establishes unavailability.

        Validate hypothetical clinic-grid candidates against the same source
        constraints used by mutations. Ignore occupancy, leave and closures:
        those are precisely the reasons a genuinely requested query can be empty.
        An empty subset of the requested window proves nothing about the rest.
        """
        from .appointment_requests import requested_operation

        operation = requested_operation(self._context().body) or 'book'
        original_date = None
        if operation == 'reschedule':
            try:
                originals = self._matching_originals()
            except (PermissionError, ValueError):
                return False
            if len(originals) != 1:
                return False
            original_date = originals[0]['date']
            if visit_type != originals[0]['visit_type']:
                return False
        policy = self.data.clinic
        duration = policy['visit_types'][visit_type]
        opening, closing = minutes(policy['hours']['open']), minutes(policy['hours']['close'])
        lunch_start, lunch_end = map(minutes, policy['hours']['lunch'])
        matched = False
        for offset in range(61):
            day = (self.data.today + timedelta(days=offset)).isoformat()
            try:
                self._check_requested_week(day, original_date)
            except ValueError:
                continue
            for begin in range(opening, closing - duration + 1, 20):
                end = begin + duration
                if begin < lunch_end and end > lunch_start:
                    continue
                slot = dict(provider=provider, date=day, start=clock(begin), end=clock(end), visit_type=visit_type)
                try:
                    validate_requested_slot(self._context().body, slot, self.data.today, policy['providers'],
                                            operation, original_date)
                except ValueError:
                    continue
                matched = True
                if not (start_date <= day <= end_date and time_from <= slot['start'] and slot['end'] <= time_until):
                    return False
        return matched

    def _check_search_selection(self, slot: dict) -> None:
        # A second model search is not human consent to a changed appointment.
        for request in self._unavailable_requests:
            if not (request['provider']==slot['provider'] and request['visit_type']==slot['visit_type']
                    and request['start_date']<=slot['date']<=request['end_date']
                    and request['time_from']<=slot['start'] and slot['end']<=request['time_until']):
                raise ValueError('Requested search was unavailable; alternatives require sender confirmation in a new message')

    def _check_requested_week(self, day: str, original_date: str | None = None) -> None:
        """Conservative boundary for explicit week phrases, not a general date parser.

        Ambiguous or conflicting text must be clarified rather than escaping this
        constraint. Reschedules anchor 'following week' to the owned booking.
        """
        text = ''.join(char for char in unicodedata.normalize('NFKD', self._context().body.casefold())
                       if not unicodedata.combining(char))
        text = text.replace('\u2019', "'").replace('\u2018', "'")
        following = re.search(r'\b(?:following week|semana siguiente)\b', text)
        next_week = re.search(r'\b(?:next week|proxima semana|semana proxima|semana que viene)\b', text)
        # A mention is not necessarily a positive request. This deliberately
        # conservative grammar sends negative/competing references to a human;
        # it must never turn an unavailable week into permission to book it.
        negative = re.search(r"\b(?:not|no|never|cannot|can't|don't|doesn't|isn't|won't|nunca|tampoco)\b", text)
        this_week = re.search(r'\b(?:this week|esta semana)\b', text)
        if (following or next_week) and (negative or this_week or (following and next_week)):
            raise ValueError('Negative or competing week references require clarification before changing an appointment')
        if following and original_date is None:
            raise ValueError('Following week requires an identified original appointment; ask for clarification')
        if original_date and following:
            reference = date.fromisoformat(original_date)
        elif next_week:
            reference = self.data.today
        else:
            return
        window = calendar_context(reference)['next_week']
        if not window['start_date'] <= day <= window['end_date']:
            raise ValueError(f"Requested week requires {window['start_date']} through {window['end_date']}; "
                             'search that range or ask for clarification before changing the appointment')

    def book(self, provider: str, day: str, start: str, visit_type: str) -> dict:
        with self._lock:
            self._action_allowed()
            self._verified()
            self._check_requested_week(day)
            slot = self._slot(provider, day, start, visit_type)
            validate_requested_slot(self._context().body,slot,self.data.today,self.data.clinic['providers'])
            self._check_search_selection(slot)
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
            validate_mutation_consent(self._context().body,'cancel')
            validate_requested_slot(self._context().body,a,self.data.today,self.data.clinic['providers'],'cancel')
            matching=self._matching_originals('cancel')
            if len(matching)!=1 or matching[0]['id']!=appointment_id:
                raise ValueError('Ambiguous appointment selection; ask for clarification before cancellation')
            a["status"] = "cancelled"
            self._record("cancel_appointment", appointment=copy.deepcopy(a))
            return copy.deepcopy(a)

    def _matching_originals(self, operation: str = 'reschedule') -> list[dict]:
        body=self._context().body
        parts=re.split(r'\b(?:to|al)\b',body,flags=re.I)
        original_text=parts[-2] if operation=='reschedule' and len(parts)>1 else (body if operation=='cancel' else '')
        types=requested_visit_types(original_text)
        appointments=self.list_appointments()
        explicit_ids={a['id'] for a in appointments if re.search(r'\b'+re.escape(a['id'])+r'\b',body,re.I)}
        matching=[]
        for candidate in appointments:
            if explicit_ids and candidate['id'] not in explicit_ids:continue
            if types and candidate['visit_type'] not in types:continue
            try:
                validate_requested_slot(original_text,candidate,self.data.today,self.data.clinic['providers'],'cancel')
            except ValueError:
                continue
            matching.append(candidate)
        return matching

    def reschedule(self, appointment_id: str, provider: str, day: str, start: str) -> dict:
        with self._lock:
            self._action_allowed()
            a = self._owned(appointment_id)
            matching=self._matching_originals()
            if len(matching)!=1 or matching[0]['id']!=appointment_id:
                raise ValueError('Ambiguous or unmatched requested original appointment; ask for clarification before rescheduling')
            self._check_requested_week(day, a['date'])
            slot = self._slot(provider, day, start, a["visit_type"], ignore=appointment_id)
            validate_requested_slot(self._context().body,slot,self.data.today,self.data.clinic['providers'],'reschedule',a['date'])
            self._check_search_selection(slot)
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
        if set(actions) & {'offer_appointment','offer_alternatives'}:
            searches=[event for event in audit['events'] if event['kind']=='slot_search']
            if not searches or not searches[-1]['slots']:
                raise ValueError('No available slots support this offer; search alternatives or actually route to front_desk')
            for slot in searches[-1]['slots'][:3]:
                try:
                    self._slot(slot['provider'],slot['date'],slot['start'],slot['visit_type'])
                except (ValueError,KeyError) as exc:
                    raise ValueError('Offered slots are no longer verified as available; search again') from exc
        queues = {r["queue"] for r in audit["routes"]}
        for action, required in {"route_to_billing": {"billing"}, "route_to_front_desk": {"front_desk"},
                "route_to_provider": {"provider_refills"}, "route_to_clinician": {"clinician_urgent", "clinician_routine"}}.items():
            if action in actions and not queues.intersection(required):
                raise ValueError("Cannot draft an unperformed handoff")
        return self.draft_reply(render_reply(actions, self.data.clinic, audit, language), language)

    def _requested_operation_available(self, operation: str) -> bool:
        """Read-only completion check; a routing reason is not ambiguity evidence.

        Reuse mutation constraints over the existing 60-day search horizon. This
        never executes a mutation or chooses a slot for the model. A genuinely
        ambiguous original or source constraint can still require staff help.
        """
        original = None
        if operation in {'cancel', 'reschedule'}:
            originals = self._matching_originals(operation)
            if len(originals) != 1:
                return False
            original = originals[0]
            if operation == 'cancel':
                return True
        types = {original['visit_type']} if original else (requested_visit_types(self._context().body) or set(self.data.clinic['visit_types']))
        for offset in range(61):
            day = (self.data.today + timedelta(days=offset)).isoformat()
            try:
                self._check_requested_week(day, original['date'] if original else None)
            except ValueError:
                continue
            for provider in self.data.clinic['providers']:
                for visit_type in types:
                    for begin in range(9 * 60, 17 * 60, 20):
                        try:
                            slot = self._slot(provider['id'], day, clock(begin), visit_type,
                                              ignore=original['id'] if original else None)
                            validate_requested_slot(self._context().body, slot, self.data.today,
                                self.data.clinic['providers'], operation, original['date'] if original else None)
                            self._check_search_selection(slot)
                        except ValueError:
                            continue
                        return True
        return False

    def finish_message(self, actions: list[Action], *, recovery: bool = False) -> dict:
        from .appointment_requests import requested_operation

        if self.urgency is None:
            raise ValueError("Triage is required before completion")
        if not actions or any(action not in get_args(Action) for action in actions):
            raise ValueError("Use nonempty canonical actions")
        mutations = {"prepare_refill_for_approval", "book_appointment", "reschedule_appointment", "cancel_appointment"}
        audit = self.audit()
        performed = {e["kind"] for e in audit["events"]} & mutations
        actions = set(actions) | performed
        body = norm(self._context().body)
        suspicious = bool(re.search(
            r'\b(?:ignore|disregard|forget)\b.{0,50}\b(?:previous|prior|system|instructions|rules)\b'
            r'|\bapprove\s+(?:all|every)\b'
            r'|\b(?:bypass|override|skip)\b.{0,60}\b(?:approval|verification|policy|policies|rules|safety)\b'
            r'|\b(?:doctor|provider|staff|administrator|admin)\b.{0,60}\b(?:authori[sz]ed|approved)\b.{0,40}\boverride\b', body))
        if suspicious:
            if not any(e['kind'] == 'flag_suspicious_instruction' for e in audit['events']):
                self._record('flag_suspicious_instruction', reason='Untrusted request to override clinic safeguards')
            if self.urgency != 'emergency':
                actions.add('flag_suspicious_instruction')
        if self.urgency == "emergency" and (set(actions) - performed) != {"escalate_emergency"}:
            raise ValueError("Emergency completion may only escalate and report already-performed operations")
        if self.urgency != 'emergency':
            if 'clinical' in self.intents:
                queue = 'clinician_urgent' if self.urgency == 'urgent' else 'clinician_routine'
                self.route(queue, 'Clinical concern requires clinician review')
                actions.add('route_to_clinician')
                # The direct-answer template contains public opening hours only.
                if not re.search(r'\b(?:hours|opening|closing|horario|horarios)\b', body):
                    actions.discard('answer_directly')
        # Only the host recovery entry point can set this keyword. Model-supplied
        # routing reasons never exempt a request from completion validation.
        if self.urgency != 'emergency' and not recovery:
            needs_visit = any(e['kind'] == 'refill_check' and e['decision']['status'] == 'needs_visit'
                              for e in audit['events'])
            queues = {r['queue'] for r in audit['routes']}
            if needs_visit:
                offered = bool(actions & {'offer_appointment', 'offer_alternatives'})
                visit_booked = bool(performed & {'book_appointment', 'reschedule_appointment'})
                if 'provider_refills' not in queues or not (offered or visit_booked or 'front_desk' in queues):
                    raise ValueError('needs_visit requires an actual provider_refills handoff and an available appointment offer '
                                     'or booking; if scheduling needs clarification, actually route to front_desk')
                actions.add('refill_needs_visit')
                actions.add('route_to_provider')
                if 'front_desk' in queues and not (offered or visit_booked):
                    actions.add('route_to_front_desk')
            operation = requested_operation(self._context().body)
            required = {'book': 'book_appointment', 'reschedule': 'reschedule_appointment',
                        'cancel': 'cancel_appointment'}.get(operation)
            # An unavailable requested search creates a genuine confirmation barrier.
            # A model's preference for offering a matching opening does not.
            if (required and self.find_patient()['verified'] and required not in performed
                    and ('front_desk' not in queues or self._requested_operation_available(operation))
                    and not (self._unavailable_requests and
                        actions & {'offer_appointment', 'offer_alternatives'})):
                raise ValueError(f'Requested {operation} is incomplete: perform {required} using a matching slot/owned booking; '
                                 'A generic front_desk handoff cannot replace an available matching operation; '
                                 'only genuine ambiguity or unavailability permits clarification. '
                                 'Only an unavailable requested search permits offering alternatives for confirmation.')
            if ('answer_directly' in actions and 'admin' not in self.intents
                    and not re.search(r'\b(?:hours|opening|closing|horario|horarios)\b', body)):
                raise ValueError('answer_directly supports public clinic hours only; complete the refill or other intent with tools')
        actions = sorted(actions)
        return {"actions": actions, "safety_error": "Emergency reclassification after administrative mutation" if self.urgency == "emergency" and performed else None,
                **self.draft_actions(actions, self.reply_language())}
