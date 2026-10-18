"""Local trusted-host MCP stdio server. Never expose without authentication."""
import argparse
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .clinic import Clinic
from .contracts import Action, Intent, Queue, Urgency
from .data import ClinicData


def create_server(data_path: Path) -> FastMCP:
    clinic = Clinic(ClinicData.load(data_path))
    server = FastMCP("Maplewood Clinic")

    @server.tool()
    def begin_message(message: dict) -> dict:
        """Trusted host only: bind original transport metadata. Never offer to the LLM."""
        return clinic.begin_message(message)

    @server.tool()
    def public_policy() -> dict:
        """Read clinic date/timezone, public hours, providers, visit types and policies. No patient records."""
        return clinic.data.clinic

    @server.tool()
    def triage_message(urgency: Urgency, intents: list[Intent]) -> dict:
        """First step: classify the entire message including buried clinical concerns. Emergency immediately alerts clinician_urgent and locks administrative actions."""
        return clinic.triage(urgency, intents)

    @server.tool()
    def find_patient() -> dict:
        """Verify sender from host-bound phone/email or supplied name+DOB and authorised caregiver records. Cannot choose another patient. Unverified returns null."""
        return clinic.find_patient()

    @server.tool()
    def list_medications() -> dict:
        """Read only the verified patient's active medications; includes controlled status and refill data."""
        return {"medications": clinic.list_medications()}

    @server.tool()
    def check_refill_eligibility(medication: str) -> dict:
        """Check policy for one active drug. Returns eligible/controlled/needs_visit/no_refills/too_early plus earliest request date. Does not prepare or approve."""
        return clinic.check_refill(medication)

    @server.tool()
    def prepare_refill_for_approval(medication: str) -> dict:
        """Prepare an eligible refill for provider approval and route to provider_refills. Never sends a prescription; refuses ineligible and emergency requests."""
        return clinic.prepare_refill(medication)

    @server.tool()
    def list_appointments() -> dict:
        """List future booked appointments only for the verified patient. Resolve ambiguous cancellation/reschedule requests before changing anything."""
        return {"appointments": clinic.list_appointments()}

    @server.tool()
    def find_appointment_slots(provider: str, start_date: str, end_date: str, visit_type: str,
                               time_from: str = "09:00", time_until: str = "17:00", limit: int = 8) -> dict:
        """Search inclusive ISO date range (max 60 days). Times HH:MM. Enforces duration, closing/lunch, weekends, holidays, leave, overlap and message receipt time. No patient identities disclosed."""
        return {"slots": clinic.find_slots(provider, start_date, end_date, visit_type, time_from, time_until, limit)}

    @server.tool()
    def book_appointment(provider: str, day: str, start: str, visit_type: str) -> dict:
        """Book a valid requested slot for the verified patient. If a specific requested time is unavailable, offer alternatives rather than booking a different time without permission."""
        return clinic.book(provider, day, start, visit_type)

    @server.tool()
    def cancel_appointment(appointment_id: str) -> dict:
        """Cancel one identified booked appointment owned by the verified patient. Ask for clarification when several bookings match."""
        return clinic.cancel(appointment_id)

    @server.tool()
    def reschedule_appointment(appointment_id: str, provider: str, day: str, start: str) -> dict:
        """Move an owned booking while preserving visit duration/type. Validates new slot atomically; failure leaves original booking unchanged."""
        return clinic.reschedule(appointment_id, provider, day, start)

    @server.tool()
    def route_to_human(queue: Queue, reason: str) -> dict:
        """Actually record a human handoff: urgent/routine clinician, provider_refills, front_desk or billing. Billing/records data are unavailable, so hand them off instead of inventing answers."""
        return clinic.route(queue, reason)

    @server.tool()
    def draft_reply(actions: list[Action], language: str = "English") -> dict:
        """Render an unsent reply from selected action enums, actual operation events and clinic policy. No free text accepted. Use English/Spanish. Emergency uses only the permitted safety template."""
        return clinic.draft_actions(actions, language)

    @server.tool()
    def message_audit() -> dict:
        """Trusted host only: actual bound patient, triage, routes and events. Never offer to LLM."""
        return clinic.audit()

    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    create_server(args.data).run(transport="stdio")


if __name__ == "__main__":
    main()
