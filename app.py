import os
import csv
import json
import urllib.request
from datetime import datetime
from typing import Optional, List, Dict

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field


# =========================
# Config & constants
# =========================
NEXA_SERVER_KEY = (os.getenv("NEXA_SERVER_KEY") or "").strip()

BREVO_API_KEY = (os.getenv("BREVO_API_KEY") or "").strip()
SMTP_FROM      = (os.getenv("SMTP_FROM") or "").strip()
NOTIFY_TO      = (os.getenv("NOTIFY_TO") or "").strip()

LEADS_FILE = "leads.csv"
BOOKED_STATUSES = {"pending", "confirmed"}   # slots with these statuses are considered taken
BUSINESS_HOURS = ("09:00", "18:00")          # informational; not enforced here


# =========================
# App init
# =========================
app = FastAPI(title="Nexa Lead API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# Models
# =========================
class Lead(BaseModel):
    name: str = Field(min_length=1)
    email: Optional[str] = None             # optional now
    phone: str = Field(min_length=5)
    service: str = Field(min_length=1)
    appointment_date: str                   # "YYYY-MM-DD"
    appointment_time: str                   # "HH:MM" (24h)


class LeadResponse(BaseModel):
    ok: bool
    message: str
    booking_status: str = "pending"
    taken: Optional[List[str]] = None       # returns taken slots when conflict happens


# =========================
# CSV helpers
# =========================
CSV_HEADER = [
    "timestamp_utc", "status", "name", "email", "phone",
    "service", "appointment_date", "appointment_time"
]

def _ensure_csv():
    new_file = not os.path.exists(LEADS_FILE)
    if new_file:
        with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)

def _row_to_dict(row: List[str]) -> Dict[str, str]:
    return {
        "timestamp_utc": row[0],
        "status": row[1],
        "name": row[2],
        "email": row[3],
        "phone": row[4],
        "service": row[5],
        "appointment_date": row[6],
        "appointment_time": row[7],
    }

def read_all_leads() -> List[Dict[str, str]]:
    _ensure_csv()
    leads: List[Dict[str, str]] = []
    with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if not row or len(row) < len(CSV_HEADER):
                continue
            leads.append(_row_to_dict(row))
    return leads

def write_lead(status: str, lead: Lead) -> None:
    _ensure_csv()
    with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.utcnow().isoformat(),
            status,
            lead.name,
            lead.email or "",
            lead.phone,
            lead.service,
            lead.appointment_date,
            lead.appointment_time,
        ])

def list_taken_slots_for_date(date_str: str) -> List[str]:
    """Return list of times (HH:MM) that are already booked (pending/confirmed) for the date."""
    taken: List[str] = []
    for r in read_all_leads():
        if r["appointment_date"] == date_str and r["status"] in BOOKED_STATUSES:
            taken.append(r["appointment_time"])
    # unique + sorted
    return sorted(list(dict.fromkeys(taken)))


# =========================
# Email (Brevo API)
# =========================
def send_via_brevo_api(subject: str, text: str) -> None:
    if not BREVO_API_KEY:
        print("❌ BREVO_API_KEY missing/empty after strip()")
        return
    if not (SMTP_FROM and NOTIFY_TO):
        print("ℹ Email disabled: SMTP_FROM or NOTIFY_TO not set")
        return

    payload = {
        "sender": {"email": SMTP_FROM},
        "to": [{"email": NOTIFY_TO}],
        "subject": subject,
        "textContent": text,
    }
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "api-key": BREVO_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"✅ Brevo API email sent, status {resp.status}")
    except Exception as e:
        print(f"❌ Brevo API email failed: {e}")


# =========================
# Guard middleware
# =========================
@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.startswith("/api"):
        header_key = request.headers.get("X-Nexa-Key", "")
        if not (NEXA_SERVER_KEY and header_key == NEXA_SERVER_KEY):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)


# =========================
# Routes
# =========================
@app.get("/")
async def root():
    return {"message": "API running."}

@app.get("/public/{path:path}")
async def public_files(path: str):
    file_path = os.path.join("public", path)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

@app.get("/api/availability")
async def availability(date: str = Query(..., description="YYYY-MM-DD")):
    """
    Returns taken time slots for the given date.
    Example: GET /api/availability?date=2025-10-03
    """
    taken = list_taken_slots_for_date(date)
    return {"date": date, "taken": taken, "hours": {"open": BUSINESS_HOURS[0], "close": BUSINESS_HOURS[1]}}

@app.post("/api/lead", response_model=LeadResponse)
async def create_lead(lead: Lead):
    # Check if the slot is already taken
    taken = list_taken_slots_for_date(lead.appointment_date)
    if lead.appointment_time in taken:
        # Conflict: do not write duplicate; return clear info and the day's taken slots
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "message": "Selected time is already booked. Please choose another slot.",
                "booking_status": "conflict",
                "taken": taken,
            },
        )

    # Save as "pending"
    write_lead("pending", lead)

    # Notify via email (non-blocking style is possible; keeping synchronous & quick)
    subject = "New Website Lead (pending)"
    body = (
        f"Name: {lead.name}\n"
        f"Email: {lead.email or '(not provided)'}\n"
        f"Phone: {lead.phone}\n"
        f"Service: {lead.service}\n"
        f"Date: {lead.appointment_date}\n"
        f"Time: {lead.appointment_time}\n"
        f"Status: pending\n"
        f"(Working hours: {BUSINESS_HOURS[0]}–{BUSINESS_HOURS[1]})"
    )
    send_via_brevo_api(subject, body)

    return {
        "ok": True,
        "message": "Lead saved. We will contact you to confirm the appointment.",
        "booking_status": "pending",
    }

@app.get("/api/test-email")
async def test_email():
    subject = "Test Email from API"
    body = "This is a test email from your Render FastAPI service."
    send_via_brevo_api(subject, body)
    return {"ok": True, "message": "Test email sent."}

# (Optional) Admin: download CSV (keep protected by X-Nexa-Key)
@app.get("/api/leads.csv")
async def download_csv():
    _ensure_csv()
    return FileResponse(LEADS_FILE, media_type="text/csv", filename="leads.csv")
