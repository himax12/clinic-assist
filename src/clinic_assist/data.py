"""Runtime data intentionally excludes messages and evaluation labels."""
import copy
import json
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass
class ClinicData:
    clinic: dict
    patients: list[dict]
    medications: list[dict]
    appointments: list[dict]

    @property
    def today(self) -> date:
        return date.fromisoformat(self.clinic["today"])

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.clinic["timezone"])

    @classmethod
    def load(cls, directory: Path) -> "ClinicData":
        raw = {name: json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
               for name in ("clinic", "patients", "medications", "appointments")}
        data = cls(**raw)
        data.validate()
        return data

    def clone(self) -> "ClinicData":
        return copy.deepcopy(self)

    def validate(self) -> None:
        _ = self.today, self.timezone
        patient_ids = [p["id"] for p in self.patients]
        if len(set(patient_ids)) != len(patient_ids):
            raise ValueError("Duplicate patient ID")
        providers = {p["id"] for p in self.clinic["providers"]}
        for patient in self.patients:
            date.fromisoformat(patient["dob"])
            date.fromisoformat(patient["last_visit"])
            if patient["primary_provider"] not in providers:
                raise ValueError("Unknown primary provider")
        for medication in self.medications:
            if medication["patient_id"] not in patient_ids:
                raise ValueError("Medication references unknown patient")
            date.fromisoformat(medication["last_filled"])
            if medication["days_supply"] <= 0 or medication["refills_remaining"] < 0:
                raise ValueError("Invalid medication supply/refills")
            if medication["prescriber"] not in providers:
                raise ValueError("Unknown prescriber")
        appointments = set()
        for appt in self.appointments:
            if appt["id"] in appointments:
                raise ValueError("Duplicate appointment ID")
            appointments.add(appt["id"])
            if appt["patient_id"] not in patient_ids:
                raise ValueError("Appointment references unknown patient")
            date.fromisoformat(appt["date"])
            if time.fromisoformat(appt["end"]) <= time.fromisoformat(appt["start"]):
                raise ValueError("Invalid appointment interval")
            if appt["provider"] not in providers or appt["visit_type"] not in self.clinic["visit_types"]:
                raise ValueError("Unknown appointment provider/type")
