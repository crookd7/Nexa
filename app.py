import os
import csv
import json
import uuid
import hmac
import hashlib
import urllib.request
from datetime import datetime
from typing import Optional, List, Dict

from fastapi import FastAPI, Request, HTTPException, Query, Form, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from itsdangerous import URLSafeSerializer


# =========================
# Config
# =========================
NEXA_SERVER_KEY  = (os.getenv("NEXA_SERVER_KEY") or "").strip()

BREVO_API_KEY    = (os.getenv("BREVO_API_KEY") or "").strip()
SMTP_FROM        = (os.getenv("SMTP_FROM") or "").strip()
NOTIFY_TO        = (os.getenv("NOTIFY_TO") or "").strip()

ADMIN_SECRET     = (os.getenv("ADMIN_SECRET") or "").strip()
PUBLIC_BASE_URL  = (os.getenv("PUBLIC_BASE_URL") or "").strip()

LEADS_FILE = "leads.csv"

BOOKED_STATUSES = {"confirmed"}
BUSINESS_HOURS  = ("09:00", "18:00")

# Admin login/session
ADMIN_USER = (os.getenv("ADMIN_USER") or "admin").strip()
ADMIN_PASS = (os.getenv("ADMIN_PASS") or "changeme").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET") or "supersecret123"
serializer = URLSafeSerializer(SESSION_SECRET, salt="admin-session")


# =========================
# FastAPI app
# =========================
app = FastAPI(title="Nexa Lead API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================
# Models
# =========================
class Lead(BaseModel):
    name: str = Field(min_length=1)
    email: Optional[str] = None
    phone: str = Field(min_length=5)
    service: str = Field(min_length=1)
    appointment_date: str
    appointment_time: str

class LeadResponse(BaseModel):
    ok: bool
    message: str
    booking_status: str = "pending"
    taken: Optional[List[str]] = None
    confirm_url: Optional[str] = None
    cancel_url: Optional[str] = None


# =========================
# CSV helpers
# =========================
CSV_HEADER = [
    "booking_id", "timestamp_utc", "status", "name", "email", "phone",
    "service", "appointment_date", "appointment_time"
]

def _ensure_csv() -> None:
    if not os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)

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
    out = []
    with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        _ = next(reader, None)
        for row in reader:
            if row and len(row) >= len(CSV_HEADER):
                out.append(_row_to_dict(row))
    return out

def write_lead(status: str, lead: Lead) -> str:
    _ensure_csv()
    booking_id = str(uuid.uuid4())
    with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
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
    rows, found = [], False
    with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        _ = next(reader, None)
        for row in reader:
            if row and row[0] == booking_id:
                row[2] = new_status
                found = True
            rows.append(row)
    if not found:
        return False
    with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerows(rows)
    return True

def list_taken_slots_for_date(date_str: str) -> List[str]:
    taken = []
    for r in read_all_leads():
        if r["appointment_date"] == date_str and r["status"] in BOOKED_STATUSES:
            taken.append(r["appointment_time"])
    return sorted(list(dict.fromkeys(taken)))


# =========================
# Token signing (email confirm/cancel)
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
# Email helpers (Brevo API)
# =========================
def send_via_brevo_api(subject: str, text: str, html: Optional[str] = None) -> None:
    if not BREVO_API_KEY or not (SMTP_FROM and NOTIFY_TO):
        return
    payload = {
        "sender": {"email": SMTP_FROM},
        "to": [{"email": NOTIFY_TO}],
        "subject": subject,
        "textContent": text,
    }
    if html:
        payload["htmlContent"] = html
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "api-key": BREVO_API_KEY},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print(f"❌ Brevo API email failed: {e}")

def build_owner_email(booking_id: str, lead: Lead, confirm_url: str, cancel_url: str):
    subject = "New Website Lead (pending)"
    text = f"""
Booking ID: {booking_id}
Name: {lead.name}
Email: {lead.email or '(not provided)'}
Phone: {lead.phone}
Service: {lead.service}
Date: {lead.appointment_date}
Time: {lead.appointment_time}
Status: pending

Confirm: {confirm_url}
Cancel:  {cancel_url}
"""
    html = f"""
    <h2>New Website Lead (pending)</h2>
    <p><b>{lead.name}</b> wants {lead.service} at {lead.appointment_date} {lead.appointment_time}</p>
    <a href="{confirm_url}" style="background:#16a34a;color:#fff;padding:8px 12px;border-radius:6px;text-decoration:none;">✓ Confirm</a>
    <a href="{cancel_url}" style="background:#ef4444;color:#fff;padding:8px 12px;border-radius:6px;text-decoration:none;">✕ Cancel</a>
    """
    return subject, text.strip(), html


# =========================
# Admin session helpers
# =========================
def create_session(user: str) -> str:
    return serializer.dumps({"user": user, "ts": datetime.utcnow().isoformat()})

def verify_session(token: str) -> bool:
    try:
        data = serializer.loads(token)
        return data.get("user") == ADMIN_USER
    except Exception:
        return False


# =========================
# Middleware
# =========================
@app.middleware("http")
async def protect(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api") or path.startswith("/admin"):
        if path.startswith("/api/lead") or path.startswith("/api/test-email"):
            # require API key
            header_key = request.headers.get("X-Nexa-Key", "")
            if not (NEXA_SERVER_KEY and header_key == NEXA_SERVER_KEY):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        elif path.startswith("/admin/login") or path.endswith("login.html"):
            return await call_next(request)
        else:
            session = request.cookies.get("admin_session")
            if not session or not verify_session(session):
                return RedirectResponse(url="/admin/login.html")
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
async def availability(date: str = Query(...)):
    taken = list_taken_slots_for_date(date)
    return {"date": date, "taken": taken, "hours": {"open": BUSINESS_HOURS[0], "close": BUSINESS_HOURS[1]}}

@app.post("/api/lead", response_model=LeadResponse)
async def create_lead(lead: Lead):
    taken = list_taken_slots_for_date(lead.appointment_date)
    if lead.appointment_time in taken:
        return JSONResponse(status_code=409, content={"ok": False, "message": "Slot already confirmed", "booking_status": "conflict", "taken": taken})
    booking_id = write_lead("pending", lead)
    confirm_url = f"{PUBLIC_BASE_URL}/confirm/{booking_id}?token={_sign('confirm', booking_id)}"
    cancel_url = f"{PUBLIC_BASE_URL}/cancel/{booking_id}?token={_sign('cancel', booking_id)}"
    subject, text, html = build_owner_email(booking_id, lead, confirm_url, cancel_url)
    send_via_brevo_api(subject, text, html)
    return {"ok": True, "message": "Lead saved. Await confirmation.", "booking_status": "pending"}

@app.get("/confirm/{booking_id}", response_class=HTMLResponse)
async def confirm_booking(booking_id: str, token: str):
    if not _verify("confirm", booking_id, token):
        return HTMLResponse("<h2>Invalid link</h2>", status_code=403)
    if not update_booking_status(booking_id, "confirmed"):
        return HTMLResponse("<h2>Booking not found</h2>", status_code=404)
    return HTMLResponse("<h2>✅ Booking confirmed</h2>")

@app.get("/cancel/{booking_id}", response_class=HTMLResponse)
async def cancel_booking(booking_id: str, token: str):
    if not _verify("cancel", booking_id, token):
        return HTMLResponse("<h2>Invalid link</h2>", status_code=403)
    if not update_booking_status(booking_id, "cancelled"):
        return HTMLResponse("<h2>Booking not found</h2>", status_code=404)
    return HTMLResponse("<h2>🗑️ Booking cancelled</h2>")

@app.get("/api/leads")
async def list_leads():
    return {"leads": read_all_leads()}

@app.post("/api/confirm/{booking_id}")
async def api_confirm_booking(booking_id: str):
    update_booking_status(booking_id, "confirmed")
    return {"ok": True, "message": "Booking confirmed"}

@app.post("/api/cancel/{booking_id}")
async def api_cancel_booking(booking_id: str):
    update_booking_status(booking_id, "cancelled")
    return {"ok": True, "message": "Booking cancelled"}

@app.post("/admin/login")
async def admin_login(username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        token = create_session(username)
        resp = RedirectResponse(url="/admin", status_code=302)
        resp.set_cookie("admin_session", token, httponly=True, max_age=3600)
        return resp
    return HTMLResponse("<h2>❌ Invalid login</h2>", status_code=403)

@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin/login.html", status_code=302)
    resp.delete_cookie("admin_session")
    return resp

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("public/admin.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/api/test-email")
async def test_email():
    send_via_brevo_api("Test Email", "Plain test", "<p><b>Test email</b></p>")
    return {"ok": True, "message": "Test email sent"}

@app.get("/api/leads.csv")
async def download_csv():
    _ensure_csv()
    return FileResponse(LEADS_FILE, media_type="text/csv", filename="leads.csv")
