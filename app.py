import os
import csv
import json
import uuid
import hmac
import hashlib
import urllib.request
from datetime import datetime
from typing import Optional, List, Dict

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, Field


# =========================
# Config & constants
# =========================
NEXA_SERVER_KEY = (os.getenv("NEXA_SERVER_KEY") or "").strip()

# Email via Brevo HTTP API (more reliable than SMTP)
BREVO_API_KEY = (os.getenv("BREVO_API_KEY") or "").strip()   # Brevo API v3 key (often starts with xkeysib-…)
SMTP_FROM      = (os.getenv("SMTP_FROM") or "").strip()      # Verified sender in Brevo
NOTIFY_TO      = (os.getenv("NOTIFY_TO") or "").strip()      # Where owner receives notifications

# Owner link signing
ADMIN_SECRET   = (os.getenv("ADMIN_SECRET") or "").strip()   # any long random string
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip()  # optional, e.g. https://yourdomain.tld

LEADS_FILE = "leads.csv"

# ONLY confirmed bookings block the calendar
BOOKED_STATUSES = {"confirmed"}
BUSINESS_HOURS = ("09:00", "18:00")  # purely informational for UI


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
    email: Optional[str] = None               # optional by request
    phone: str = Field(min_length=5)
    service: str = Field(min_length=1)
    appointment_date: str                     # "YYYY-MM-DD"
    appointment_time: str                     # "HH:MM" (24h)


class LeadResponse(BaseModel):
    ok: bool
    message: str
    booking_status: str = "pending"
    taken: Optional[List[str]] = None


# =========================
# CSV helpers
# =========================
CSV_HEADER = [
    "booking_id", "timestamp_utc", "status", "name", "email", "phone",
    "service", "appointment_date", "appointment_time"
]

def _ensure_csv() -> None:
    new_file = not os.path.exists(LEADS_FILE)
    if new_file:
        with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)

def _row_to_dict(row: List[str]) -> Dict[str, str]:
    return {
        "booking_id": row[0],
        "timestamp_utc": row[1],
        "status": row[2],
        "name": row[3],
        "email": row[4],
        "phone": row[5],
        "service": row[6],
        "appointment_date": row[7],
        "appointment_time": row[8],
    }

def read_all_leads() -> List[Dict[str, str]]:
    _ensure_csv()
    leads: List[Dict[str, str]] = []
    with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        _ = next(reader, None)  # header
        for row in reader:
            if not row or len(row) < len(CSV_HEADER):
                continue
            leads.append(_row_to_dict(row))
    return leads

def write_lead(status: str, lead: Lead) -> str:
    _ensure_csv()
    booking_id = str(uuid.uuid4())
    with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            booking_id,
            datetime.utcnow().isoformat(),
            status,
            lead.name,
            lead.email or "",
            lead.phone,
            lead.service,
            lead.appointment_date,
            lead.appointment_time,
        ])
    return booking_id

def update_booking_status(booking_id: str, new_status: str) -> bool:
    if not os.path.exists(LEADS_FILE):
        return False
    rows = []
    found = False
    with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if not row:
                continue
            if row[0] == booking_id:
                row[2] = new_status  # status column
                found = True
            rows.append(row)
    if not found:
        return False
    with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)
    return True

def list_taken_slots_for_date(date_str: str) -> List[str]:
    """Return list of times (HH:MM) that are already CONFIRMED for the date."""
    taken: List[str] = []
    for r in read_all_leads():
        if r["appointment_date"] == date_str and r["status"] in BOOKED_STATUSES:
            taken.append(r["appointment_time"])
    return sorted(list(dict.fromkeys(taken)))


# =========================
# Link signing (confirm/cancel)
# =========================
def _sign(action: str, booking_id: str) -> str:
    if not ADMIN_SECRET:
        return ""
    msg = f"{action}:{booking_id}".encode("utf-8")
    return hmac.new(ADMIN_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()

def _verify(action: str, booking_id: str, token: str) -> bool:
    if not ADMIN_SECRET or not token:
        return False
    expected = _sign(action, booking_id)
    return hmac.compare_digest(expected, token)


# =========================
# Email (Brevo API)
# =========================
def send_via_brevo_api(subject: str, text: str) -> None:
    """Send email to NOTIFY_TO using Brevo HTTP API. Non-fatal on failure."""
    if not BREVO_API_KEY:
        print("❌ BREVO_API_KEY missing/empty")
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
    # Protect all /api/* endpoints with X-Nexa-Key header
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
    Returns taken time slots for the given date (only CONFIRMED bookings block).
    """
    taken = list_taken_slots_for_date(date)
    return {"date": date, "taken": taken, "hours": {"open": BUSINESS_HOURS[0], "close": BUSINESS_HOURS[1]}}

@app.post("/api/lead", response_model=LeadResponse)
async def create_lead(lead: Lead):
    # Conflict only if the exact slot is already CONFIRMED
    taken = list_taken_slots_for_date(lead.appointment_date)
    if lead.appointment_time in taken:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "message": "Selected time is already confirmed. Please choose another slot.",
                "booking_status": "conflict",
                "taken": taken,
            },
        )

    # Save as "pending" (soft hold; multiple pendings for same slot allowed)
    booking_id = write_lead("pending", lead)

    # Signed owner links (confirm / cancel)
    confirm_token = _sign("confirm", booking_id)
    cancel_token  = _sign("cancel", booking_id)
    base = PUBLIC_BASE_URL or ""  # optional absolute base
    confirm_url = f"{base}/confirm/{booking_id}?token={confirm_token}"
    cancel_url  = f"{base}/cancel/{booking_id}?token={cancel_token}"

    # Notify owner
    subject = "New Website Lead (pending)"
    body = (
        f"Booking ID: {booking_id}\n"
        f"Name: {lead.name}\n"
        f"Email: {lead.email or '(not provided)'}\n"
        f"Phone: {lead.phone}\n"
        f"Service: {lead.service}\n"
        f"Date: {lead.appointment_date}\n"
        f"Time: {lead.appointment_time}\n"
        f"Status: pending\n\n"
        "Note: Pending bookings do NOT block the calendar. Only confirmed bookings do.\n\n"
        "Owner actions:\n"
        f"✓ Confirm: {confirm_url}\n"
        f"✕ Cancel:  {cancel_url}\n"
    )
    send_via_brevo_api(subject, body)

    return {
        "ok": True,
        "message": "Lead saved. We will contact you to confirm the appointment.",
        "booking_status": "pending",
    }

@app.get("/confirm/{booking_id}", response_class=HTMLResponse)
async def confirm_booking(booking_id: str, token: str):
    if not _verify("confirm", booking_id, token):
        return HTMLResponse("<h2>Invalid or expired confirmation link.</h2>", status_code=403)

    leads = read_all_leads()
    target = next((r for r in leads if r["booking_id"] == booking_id), None)
    if not target:
        return HTMLResponse("<h2>Booking not found.</h2>", status_code=404)

    if target["status"] == "confirmed":
        return HTMLResponse("<h2>✅ Already confirmed.</h2>")

    # Refuse confirm if another booking is already confirmed for same slot
    for r in leads:
        if (
            r["booking_id"] != booking_id and
            r["appointment_date"] == target["appointment_date"] and
            r["appointment_time"] == target["appointment_time"] and
            r["status"] == "confirmed"
        ):
            msg = (
                "<h2>⚠️ Cannot confirm.</h2>"
                "<p>This time slot is already <b>confirmed</b> for another booking.</p>"
                "<p>Please choose a different time with the client or cancel this pending request.</p>"
            )
            return HTMLResponse(msg, status_code=409)

    ok = update_booking_status(booking_id, "confirmed")
    if not ok:
        return HTMLResponse("<h2>Booking not found.</h2>", status_code=404)
    return HTMLResponse("<h2>✅ Booking confirmed. This slot is now reserved.</h2>")

@app.get("/cancel/{booking_id}", response_class=HTMLResponse)
async def cancel_booking(booking_id: str, token: str):
    if not _verify("cancel", booking_id, token):
        return HTMLResponse("<h2>Invalid or expired cancellation link.</h2>", status_code=403)
    ok = update_booking_status(booking_id, "cancelled")
    if not ok:
        return HTMLResponse("<h2>Booking not found.</h2>", status_code=404)
    return HTMLResponse("<h2>🗑️ Booking cancelled. The slot is now free.</h2>")

@app.get("/api/test-email")
async def test_email():
    subject = "Test Email from API"
    body = "This is a test email from your Render FastAPI service."
    send_via_brevo_api(subject, body)
    return {"ok": True, "message": "Test email sent."}

# Admin: download CSV (protected by the header via middleware)
@app.get("/api/leads.csv")
async def download_csv():
    _ensure_csv()
    return FileResponse(LEADS_FILE, media_type="text/csv", filename="leads.csv")
