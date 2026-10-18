"""Constrained unsent drafts: no free-form clinical text or invented operations."""


def render_reply(actions: list[str], policy: dict, audit: dict, language: str) -> str:
    es = language.casefold() == "spanish"
    if audit["urgency"] == "emergency":
        return ("Llame al 911 o vaya al servicio de urgencias más cercano. Se ha alertado a la clínica." if es else
                "Call 911 or go to the nearest emergency department. The clinic has been alerted.")
    parts = []
    phrases = {
        "prepare_refill_for_approval": ("Su solicitud de renovación está pendiente de aprobación del proveedor.", "Your refill request is pending provider approval."),
        "refill_needs_visit": ("Se necesita una consulta antes de preparar la renovación.", "A clinic visit is needed before this refill can be prepared."),
        "route_to_provider": ("Se ha enviado su mensaje al equipo de renovaciones para revisión del proveedor.", "Your message has been routed to the provider refill team."),
        "route_to_clinician": ("Se ha enviado su mensaje al equipo clínico para revisión.", "Your message has been routed to a clinician for review."),
        "route_to_billing": ("Se ha enviado su consulta a facturación; no disponemos aquí de su saldo ni copago.", "Your question has been routed to billing; balance and copay information are not available here."),
        "route_to_front_desk": ("Se ha enviado su solicitud a recepción para revisión y seguimiento.", "Your request has been routed to the front desk for review and follow-up."),
        "request_verification": ("Necesitamos verificar su identidad antes de acceder a información personal. Use su contacto registrado o confirme su nombre y fecha de nacimiento.", "We need to verify your identity before accessing personal information. Use your contact on file or confirm your name and date of birth."),
        "decline_disclosure": ("No podemos revelar información de otra persona sin verificar la autorización.", "We cannot disclose another person's information without verifying authorisation."),
        "offer_new_patient_booking": ("Recepción puede ayudarle con el registro y una cita de paciente nuevo.", "The front desk can help with registration and a new-patient appointment."),
        "flag_suspicious_instruction": ("Las solicitudes deben seguir las normas de verificación y aprobación de la clínica.", "Requests must follow the clinic's verification and approval policies."),
        "acknowledge": ("Gracias por su mensaje.", "Thank you for your message."),
    }
    for action in actions:
        if action in phrases:
            parts.append(phrases[action][0 if es else 1])
        elif action in {"book_appointment", "reschedule_appointment", "cancel_appointment"}:
            events = [e for e in audit["events"] if e["kind"] == action]
            if not events:
                raise ValueError("Cannot describe an unperformed appointment change")
            a = events[-1]["appointment"]
            status = {"book_appointment": ("reservada", "booked"), "reschedule_appointment": ("reprogramada", "rescheduled"),
                      "cancel_appointment": ("cancelada", "cancelled")}[action][0 if es else 1]
            parts.append((f"Cita {status}: " if es else f"Appointment {status}: ") + f"{a['date']} {a['start']}-{a['end']} ({a['provider']}).")
        elif action == "refill_too_early":
            checks = [e for e in audit["events"] if e["kind"] == "refill_check" and e["decision"]["status"] == "too_early"]
            if not checks:
                raise ValueError("No too-early refill check supports this reply")
            day = checks[-1]["decision"]["earliest_request_date"]
            parts.append(("La fecha más temprana para preparar la solicitud es " if es else "The earliest date this request can be prepared is ") + day + ".")
        elif action == "answer_directly":
            h = policy["hours"]
            parts.append(("Horario de la clínica: " if es else "Clinic hours: ") + ", ".join(h["days"]) + f" {h['open']}-{h['close']}; "
                         + ("almuerzo " if es else "lunch ") + "-".join(h["lunch"]) + ". "
                         + ("Cerrado en estas fechas: " if es else "Closed on these holidays: ") + ", ".join(policy["holidays"]) + ".")
        elif action in {"offer_appointment", "offer_alternatives"}:
            searches = [e for e in audit["events"] if e["kind"] == "slot_search"]
            slots = searches[-1]["slots"] if searches else []
            if slots:
                parts.append(("Opciones disponibles, sujetas a confirmación: " if es else "Available options, subject to confirmation: ")
                    + "; ".join(f"{s['date']} {s['start']}-{s['end']} ({s['provider']})" for s in slots[:3]) + ".")
            else:
                parts.append("Recepción puede ayudarle a encontrar una cita." if es else "The front desk can help find an appointment.")
    return " ".join(parts)
