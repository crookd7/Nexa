import os
import csv
import json
import uuid
import hmac
import hashlib
import urllib.request
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from fastapi import FastAPI, Request, HTTPException, Query, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    HTMLResponse,
    RedirectResponse,
)
from pydantic import BaseModel, Field
from itsdangerous import URLSafeSerializer


# =========================
# Environment / Config
# =========================
# Public form key (used by /api/lead)
NEXA_SERVER_KEY = (os.getenv("NEXA_SERVER_KEY") or "").strip()

# Brevo HTTP API (NOT SMTP)
BREVO_API_KEY = (os.getenv("BREVO_API_KEY") or "").strip()
SMTP_FROM = (os.getenv("SMTP_FROM") or "").strip()      # verified sender in Brevo
NOTIFY_TO = (os.getenv("NOTIFY_TO") or "").strip()      # where owner receives notifications

# Email confirm/cancel link signing
ADMIN_SECRET = (os.getenv("ADMIN_SECRET") or "").strip()  # long random string
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip()  # e.g. https://yourapp.onrender.com

# Admin session login
ADMIN_USER = (os.getenv("ADMIN_USER") or "admin").strip()
ADMIN_PASS = (os.getenv("ADMIN_PASS") or "changeme").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET") or "supersecret123"
serializer = URLSafeSerializer(SESSION_SECRET, salt="admin-session")

# Optional: OpenAI key (not required; chatbot is rule-based if missing)
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()

# Data
LEADS_FILE = "leads.csv"
CSV_HEADER = [
    "booking_id", "timestamp_utc", "status", "name", "email", "phone",
    "service", "appointment_date", "appointment_time"
]

# Availability behavior: only confirmed blocks
BOOKED_STATUSES = {"confirmed"}
BUSINESS_HOURS = ("09:00", "18:00")  # UI hint only


# =========================
# FastAPI app
# =========================
app = FastAPI(title="Nexa Lead API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten later if you have a fixed domain
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
    appointment_date: str  # "YYYY-MM-DD"
    appointment_time: str  # "HH:MM"

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
    out: List[Dict[str, str]] = []
    with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        _ = next(reader, None)  # header
        for row in reader:
            if not row or len(row) < len(CSV_HEADER):
                continue
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
    rows: List[List[str]] = []
    found = False
    with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        _ = next(reader, None)
        for row in reader:
            if not row:
                continue
            if row[0] == booking_id:
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
    """Return times (HH:MM) that are already CONFIRMED for the date."""
    taken: List[str] = []
    for r in read_all_leads():
        if r["appointment_date"] == date_str and r["status"] in BOOKED_STATUSES:
            taken.append(r["appointment_time"])
    # unique + sorted
    return sorted(list(dict.fromkeys(taken)))

def list_pending_slots_for_date(date_str: str) -> List[str]:
    pending: List[str] = []
    for r in read_all_leads():
        if r["appointment_date"] == date_str and r["status"] == "pending":
            pending.append(r["appointment_time"])
    return sorted(list(dict.fromkeys(pending)))


# =========================
# Token signing (for email confirm/cancel)
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

def build_owner_email(booking_id: str, lead: Lead, confirm_url: str, cancel_url: str):
    subject = "New Website Lead (pending)"
    text = (
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
    html = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;line-height:1.5;color:#0f172a">
      <h2 style="margin:0 0 8px">New Website Lead <small style="color:#64748b">(pending)</small></h2>
      <table style="border-collapse:collapse;margin-top:8px">
        <tr><td style="padding:4px 8px;color:#64748b">Booking ID:</td><td style="padding:4px 8px">{booking_id}</td></tr>
        <tr><td style="padding:4px 8px;color:#64748b">Name:</td><td style="padding:4px 8px">{lead.name}</td></tr>
        <tr><td style="padding:4px 8px;color:#64748b">Email:</td><td style="padding:4px 8px">{lead.email or '(not provided)'}</td></tr>
        <tr><td style="padding:4px 8px;color:#64748b">Phone:</td><td style="padding:4px 8px">{lead.phone}</td></tr>
        <tr><td style="padding:4px 8px;color:#64748b">Service:</td><td style="padding:4px 8px">{lead.service}</td></tr>
        <tr><td style="padding:4px 8px;color:#64748b">Date:</td><td style="padding:4px 8px">{lead.appointment_date}</td></tr>
        <tr><td style="padding:4px 8px;color:#64748b">Time:</td><td style="padding:4px 8px">{lead.appointment_time}</td></tr>
        <tr><td style="padding:4px 8px;color:#64748b">Status:</td><td style="padding:4px 8px">pending</td></tr>
      </table>

      <p style="margin-top:12px;color:#475569">
        Note: Pending bookings do <b>not</b> block the calendar. Only <b>confirmed</b> bookings do.
      </p>

      <div style="margin-top:16px">
        <a href="{confirm_url}"
           style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;
                  padding:10px 14px;border-radius:8px;font-weight:700;margin-right:8px">
          ✓ Confirm
        </a>
        <a href="{cancel_url}"
           style="display:inline-block;background:#ef4444;color:#fff;text-decoration:none;
                  padding:10px 14px;border-radius:8px;font-weight:700">
          ✕ Cancel
        </a>
      </div>

      <p style="margin-top:12px;color:#64748b;font-size:13px">
        If buttons don't work, copy a link:<br/>
        Confirm: <a href="{confirm_url}">{confirm_url}</a><br/>
        Cancel: <a href="{cancel_url}">{cancel_url}</a>
      </p>
    </div>
    """
    return subject, text, html


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
# Middleware (auth routing)
# =========================
@app.middleware("http")
async def protect(request: Request, call_next):
    path = request.url.path

    # Public read-only API so the public page can load
    if path.startswith("/api/availability") or path.startswith("/api/chat"):
        return await call_next(request)

    # Lead submission is protected by header key (from public form)
    if path.startswith("/api/lead"):
        header_key = request.headers.get("X-Nexa-Key", "")
        if not (NEXA_SERVER_KEY and header_key == NEXA_SERVER_KEY):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

    # Allow admin login page + POST
    if path.startswith("/admin/login") or path.endswith("/admin/login.html"):
        return await call_next(request)

    # Admin pages & admin APIs require a valid session cookie
    if path.startswith("/api") or path.startswith("/admin"):
        session = request.cookies.get("admin_session")
        if not session or not verify_session(session):
            return RedirectResponse(url="/admin/login.html")
        return await call_next(request)

    # Everything else (public/static files)
    return await call_next(request)


# =========================
# Routes
# =========================
# Redirect root to your public page
@app.get("/")
async def root():
    return RedirectResponse(url="/public/index.html", status_code=302)

# Serve any file under /public/*
@app.get("/public/{path:path}")
async def public_files(path: str):
    file_path = os.path.join("public", path)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

# Explicit route for admin login HTML
@app.get("/admin/login.html", response_class=HTMLResponse)
async def admin_login_page():
    path = os.path.join("public", "admin", "login.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)

# Public availability (confirmed + pending info)
@app.get("/api/availability")
async def availability(date: str = Query(..., description="YYYY-MM-DD")):
    taken = list_taken_slots_for_date(date)
    pending = list_pending_slots_for_date(date)
    return {
        "date": date,
        "taken": taken,               # confirmed
        "pending": pending,           # pending requests (informational)
        "hours": {"open": BUSINESS_HOURS[0], "close": BUSINESS_HOURS[1]},
    }

# Lead submission (public form → header key required by middleware)
@app.post("/api/lead", response_model=LeadResponse)
async def create_lead(lead: Lead):
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

    booking_id = write_lead("pending", lead)
    confirm_token = _sign("confirm", booking_id)
    cancel_token = _sign("cancel", booking_id)
    base = PUBLIC_BASE_URL or ""
    confirm_url = f"{base}/confirm/{booking_id}?token={confirm_token}"
    cancel_url = f"{base}/cancel/{booking_id}?token={cancel_token}"

    subject, text, html = build_owner_email(booking_id, lead, confirm_url, cancel_url)
    send_via_brevo_api(subject, text, html)

    return {
        "ok": True,
        "message": "Lead saved. We will contact you to confirm the appointment.",
        "booking_status": "pending",
        # returning links is handy while testing
        "confirm_url": confirm_url,
        "cancel_url": cancel_url,
    }

# Email-confirm via token (public but safe because token is HMAC signed)
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

    # Avoid double-confirm: refuse if another booking already confirmed for same slot
    for r in leads:
        if (
            r["booking_id"] != booking_id
            and r["appointment_date"] == target["appointment_date"]
            and r["appointment_time"] == target["appointment_time"]
            and r["status"] == "confirmed"
        ):
            msg = (
                "<h2>⚠️ Cannot confirm.</h2>"
                "<p>This time slot is already <b>confirmed</b> for another booking.</p>"
                "<p>Please choose a different time with the client or cancel this pending request.</p>"
            )
            return HTMLResponse(msg, status_code=409)

    if not update_booking_status(booking_id, "confirmed"):
        return HTMLResponse("<h2>Booking not found.</h2>", status_code=404)
    return HTMLResponse("<h2>✅ Booking confirmed. This slot is now reserved.</h2>")

# Email-cancel via token
@app.get("/cancel/{booking_id}", response_class=HTMLResponse)
async def cancel_booking(booking_id: str, token: str):
    if not _verify("cancel", booking_id, token):
        return HTMLResponse("<h2>Invalid or expired cancellation link.</h2>", status_code=403)
    if not update_booking_status(booking_id, "cancelled"):
        return HTMLResponse("<h2>Booking not found.</h2>", status_code=404)
    return HTMLResponse("<h2>🗑️ Booking cancelled. The slot is now free.</h2>")

# ===== Admin-only APIs (session cookie required by middleware) =====
@app.get("/api/leads")
async def list_leads():
    return {"leads": read_all_leads()}

@app.post("/api/confirm/{booking_id}")
async def api_confirm_booking(booking_id: str):
    # double-booking guard
    leads = read_all_leads()
    target = next((r for r in leads if r["booking_id"] == booking_id), None)
    if not target:
        return JSONResponse({"ok": False, "message": "Booking not found"}, status_code=404)
    if target["status"] == "confirmed":
        return {"ok": True, "message": "Already confirmed"}
    for r in leads:
        if (
            r["booking_id"] != booking_id
            and r["appointment_date"] == target["appointment_date"]
            and r["appointment_time"] == target["appointment_time"]
            and r["status"] == "confirmed"
        ):
            return JSONResponse({"ok": False, "message": "Time slot already confirmed for another booking."}, status_code=409)
    update_booking_status(booking_id, "confirmed")
    return {"ok": True, "message": "Booking confirmed"}

@app.post("/api/cancel/{booking_id}")
async def api_cancel_booking(booking_id: str):
    ok = update_booking_status(booking_id, "cancelled")
    if not ok:
        return JSONResponse({"ok": False, "message": "Booking not found"}, status_code=404)
    return {"ok": True, "message": "Booking cancelled"}

# Admin login/logout + page
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
    # This serves the admin table UI (if you keep a separate admin.html)
    path = os.path.join("public", "admin.html")
    if not os.path.isfile(path):
        # If you don't have a separate admin.html, you can show a small placeholder
        return HTMLResponse("<h2>Admin dashboard is embedded in the public page (open the 🔑 Admin panel).</h2>")
    return FileResponse(path)

# Optional: download CSV (session-protected by middleware)
@app.get("/api/leads.csv")
async def download_csv():
    _ensure_csv()
    return FileResponse(LEADS_FILE, media_type="text/csv", filename="leads.csv")


# =========================
# Chatbot endpoint (public)
# =========================
# Minimal NL parsing to access availability & create bookings from chat.
# If OPENAI_API_KEY is present, we can rephrase responses via OpenAI (optional niceness).
DATE_RX = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
TIME_RX = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\b")

def _nice_reply(text: str) -> str:
    """Optionally send to OpenAI for a nicer phrasing."""
    if not OPENAI_API_KEY:
        return text
    try:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are a concise, friendly booking assistant. Keep replies under 120 words."},
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {OPENAI_API_KEY}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"OpenAI nicening failed: {e}")
        return text

@app.post("/api/chat")
async def chat(payload: Dict[str, str]):
    """
    Public chatbot. Understands:
      - availability: "free slots on 2025-10-05", "availability 2025-10-05"
      - booking: "book me NAME phone +359... service haircut on 2025-10-05 at 14:30"
    Extracts date/time/name/phone/service when possible.
    Creates PENDING booking (owner confirms via email or admin panel).
    """
    msg = (payload.get("message") or "").strip()
    if not msg:
        return {"reply": "Hi! Ask me about availability (e.g. 'availability 2025-10-05') or say 'book me ...' with your name, phone, service, date and time."}

    low = msg.lower()

    # 1) Availability intent
    if "avail" in low or "free" in low or "slots" in low:
        m = DATE_RX.search(msg)
        if not m:
            base = "Please tell me the date like 2025-10-05."
            return {"reply": _nice_reply(base)}
        date_str = m.group(1)
        taken = list_taken_slots_for_date(date_str)
        pending = list_pending_slots_for_date(date_str)
        if not taken and not pending:
            base = f"{date_str}: All times look open between {BUSINESS_HOURS[0]} and {BUSINESS_HOURS[1]}."
        else:
            t = ", ".join(taken) if taken else "none"
            p = ", ".join(pending) if pending else "none"
            base = f"{date_str} — Confirmed (blocked): {t}. Pending requests: {p}. Tell me a time and I can tentatively book you."
        return {"reply": _nice_reply(base)}

    # 2) Booking intent (very simple extraction)
    if "book" in low or "schedule" in low or "appointment" in low:
        # extract fields
        date_m = DATE_RX.search(msg)
        time_m = TIME_RX.search(msg)
        name_m = re.search(r"(?:i am|i'm|name is)\s+([^\.,\n]+)", low) or re.search(r"\bname\s*:\s*([^\.,\n]+)", low)
        phone_m = re.search(r"(?:phone|tel|mobile|gsm)\s*[:\-]?\s*([\+\d][\d\s\-]{6,})", low)
        service_m = re.search(r"(?:service|for|need|want)\s+([a-zA-Zа-яА-Я0-9 \-_/]{2,})", msg)

        if not (date_m and time_m):
            return {"reply": _nice_reply("Please include date (YYYY-MM-DD) and time (HH:MM). For example: 'book me for haircut on 2025-10-05 at 14:30'.")}

        date_str = date_m.group(1)
        time_str = f"{time_m.group(1)}:{time_m.group(2)}"

        # Defaults if missing
        name = (name_m.group(1).strip() if name_m else "Guest").title()
        phone = (phone_m.group(1).strip() if phone_m else "unknown")
        service = (service_m.group(1).strip() if service_m else "service")

        # Conflict check against confirmed
        taken = list_taken_slots_for_date(date_str)
        if time_str in taken:
            base = f"That time ({date_str} {time_str}) is already confirmed. Try another time."
            return {"reply": _nice_reply(base)}

        # Create pending lead
        lead = Lead(
            name=name,
            email=None,
            phone=phone,
            service=service,
            appointment_date=date_str,
            appointment_time=time_str,
        )
        booking_id = write_lead("pending", lead)

        # Send owner email
        confirm_token = _sign("confirm", booking_id)
        cancel_token = _sign("cancel", booking_id)
        base_url = PUBLIC_BASE_URL or ""
        confirm_url = f"{base_url}/confirm/{booking_id}?token={confirm_token}"
        cancel_url = f"{base_url}/cancel/{booking_id}?token={cancel_token}"
        subject, text, html = build_owner_email(booking_id, lead, confirm_url, cancel_url)
        send_via_brevo_api(subject, text, html)

        base = (
            f"Done! I created a pending booking for {name} on {date_str} at {time_str} for '{service}'. "
            "The owner will confirm shortly; if accepted, the slot will be blocked."
        )
        return {"reply": _nice_reply(base)}

    # 3) Fallback
    help_text = (
        "I can check availability or tentatively book you. "
        "Examples:\n"
        "• availability 2025-10-05\n"
        "• book me for haircut on 2025-10-05 at 14:30, I'm Alex, phone +359..."
    )
    return {"reply": _nice_reply(help_text)}
