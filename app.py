import os, csv, json, uuid, hmac, hashlib, urllib.request, re
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from itsdangerous import URLSafeSerializer

# -------------------------
# Environment / Config
# -------------------------
NEXA_SERVER_KEY = (os.getenv("NEXA_SERVER_KEY") or "").strip()

BREVO_API_KEY = (os.getenv("BREVO_API_KEY") or "").strip()
SMTP_FROM      = (os.getenv("SMTP_FROM") or "").strip()
NOTIFY_TO      = (os.getenv("NOTIFY_TO") or "").strip()

ADMIN_SECRET   = (os.getenv("ADMIN_SECRET") or "").strip()
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip()

ADMIN_USER = (os.getenv("ADMIN_USER") or "admin").strip()
ADMIN_PASS = (os.getenv("ADMIN_PASS") or "changeme").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET") or "supersecret123"
serializer = URLSafeSerializer(SESSION_SECRET, salt="admin-session")

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()

BUSINESS_NAME = (os.getenv("BUSINESS_NAME") or "Nexa").strip()
BUSINESS_DESC = (os.getenv("BUSINESS_DESC") or
                 "We provide consultations and scheduling for clients in Sofia.").strip()
LOGO_URL      = (os.getenv("LOGO_URL") or "").strip()

# Payment link for clients (optional)
PAYMENT_LINK_BASE = (os.getenv("PAYMENT_LINK_BASE") or "").strip()
PROMO_CODE        = (os.getenv("PROMO_CODE") or "NEXA10").strip()

# Data
LEADS_FILE = os.getenv("LEADS_FILE") or "leads.csv"
CSV_HEADER = [
    "booking_id", "timestamp_utc", "status", "name", "email", "phone",
    "service", "appointment_date", "appointment_time", "paid"
]
BOOKED_STATUSES = {"confirmed"}
BUSINESS_HOURS = ("09:00", "18:00")

# -------------------------
# App
# -------------------------
app = FastAPI(title="Nexa Lead API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# Models
# -------------------------
class Lead(BaseModel):
    name: str = Field(min_length=1)
    email: Optional[str] = None
    phone: str = Field(min_length=5)
    service: str = Field(min_length=1)
    appointment_date: str  # YYYY-MM-DD
    appointment_time: str  # HH:MM

class LeadResponse(BaseModel):
    ok: bool
    message: str
    booking_status: str = "pending"
    taken: Optional[List[str]] = None
    confirm_url: Optional[str] = None
    cancel_url: Optional[str] = None

# -------------------------
# CSV helpers
# -------------------------
def _ensure_csv() -> None:
    created = False
    if not os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)
        created = True
        print(f"📄 Created CSV {LEADS_FILE}")

    # upgrade header if missing "paid"
    try:
        with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
            rd = csv.reader(f)
            header = next(rd, None)
            rows = list(rd)
        if header is None:
            return
        if header == CSV_HEADER:
            return
        # pad rows and rewrite to new header
        new_rows = []
        for r in rows:
            r = (r + [""])[:len(CSV_HEADER)]
            new_rows.append(r)
        with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f); wr.writerow(CSV_HEADER); wr.writerows(new_rows)
        if not created:
            print("🔧 Upgraded CSV schema ->", CSV_HEADER)
    except Exception as e:
        print("CSV check/upgrade skipped:", e)

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
        "paid": (row[9] if len(row) > 9 else ""),
    }

def read_all_leads() -> List[Dict[str, str]]:
    _ensure_csv()
    out: List[Dict[str, str]] = []
    with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
        rd = csv.reader(f)
        _ = next(rd, None)
        for row in rd:
            if not row:
                continue
            row = (row + [""])[:len(CSV_HEADER)]
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
            "",  # paid
        ])
    return booking_id

def update_booking_status(booking_id: str, new_status: str) -> bool:
    if not os.path.exists(LEADS_FILE):
        return False
    rows: List[List[str]] = []
    found = False
    with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
        rd = csv.reader(f)
        _ = next(rd, None)
        for row in rd:
            if not row:
                continue
            row = (row + [""])[:len(CSV_HEADER)]
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

def set_paid(booking_id: str, make_paid: bool) -> bool:
    if not os.path.exists(LEADS_FILE):
        return False
    rows: List[List[str]] = []
    found = False
    with open(LEADS_FILE, "r", newline="", encoding="utf-8") as f:
        rd = csv.reader(f); _ = next(rd, None)
        for row in rd:
            if not row:
                continue
            row = (row + [""])[:len(CSV_HEADER)]
            if row[0] == booking_id:
                row[9] = "yes" if make_paid else ""
                found = True
            rows.append(row)
    if not found:
        return False
    with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f); wr.writerow(CSV_HEADER); wr.writerows(rows)
    return True

def list_taken_slots_for_date(date_str: str) -> List[str]:
    taken: List[str] = []
    for r in read_all_leads():
        if r["appointment_date"] == date_str and r["status"] in BOOKED_STATUSES:
            taken.append(r["appointment_time"])
    return sorted(list(dict.fromkeys(taken)))

def list_pending_slots_for_date(date_str: str) -> List[str]:
    pending: List[str] = []
    for r in read_all_leads():
        if r["appointment_date"] == date_str and r["status"] == "pending":
            pending.append(r["appointment_time"])
    return sorted(list(dict.fromkeys(pending)))

# -------------------------
# Token signing
# -------------------------
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

# -------------------------
# Email via Brevo HTTP API
# -------------------------
def send_via_brevo_api(subject: str, text: str, html: Optional[str] = None, to_email: Optional[str] = None) -> None:
    if not BREVO_API_KEY or not (SMTP_FROM and (to_email or NOTIFY_TO)):
        return
    payload = {
        "sender": {"email": SMTP_FROM, "name": BUSINESS_NAME},
        "to": [{"email": (to_email or NOTIFY_TO)}],
        "subject": subject,
        "textContent": text,
    }
    if html:
        payload["htmlContent"] = html
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json","Accept": "application/json","api-key": BREVO_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"✅ Brevo email sent: {resp.status}")
    except Exception as e:
        print(f"❌ Brevo email failed: {e}")

def _wrap_email_html(title: str, inner_html: str) -> str:
    return f"""<div style="font-family:Arial,Helvetica,sans-serif;line-height:1.6;color:#0f172a">
  <h2 style="margin:0 0 8px">{title}</h2>
  {inner_html}
</div>"""

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
        "Owner actions:\n"
        f"✓ Confirm: {confirm_url}\n"
        f"✕ Cancel:  {cancel_url}\n"
    )
    html = f"""
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
      <div style="margin-top:16px">
        <a href="{confirm_url}" style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:700;margin-right:8px">✓ Confirm</a>
        <a href="{cancel_url}" style="display:inline-block;background:#ef4444;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:700">✕ Cancel</a>
      </div>
    """
    return subject, text, _wrap_email_html("New Website Lead (pending)", html)

# -------------------------
# Admin session helpers
# -------------------------
def create_session(user: str) -> str:
    return serializer.dumps({"user": user, "ts": datetime.utcnow().isoformat()})

def verify_session(token: str) -> bool:
    try:
        data = serializer.loads(token)
        return data.get("user") == ADMIN_USER
    except Exception:
        return False

# -------------------------
# Middleware
# -------------------------
@app.middleware("http")
async def protect(request: Request, call_next):
    path = request.url.path

    # public APIs
    if (path.startswith("/api/availability")
        or path.startswith("/api/chat")
        or path.startswith("/api/chat-contact")
        or path.startswith("/api/brand")):
        return await call_next(request)

    # public lead submit
    if path == "/api/lead" or path.startswith("/api/lead/"):
        header_key = request.headers.get("X-Nexa-Key", "")
        if NEXA_SERVER_KEY and header_key != NEXA_SERVER_KEY:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    # admin login page & login POST are public
    if (path.startswith("/admin/login")
        or path.endswith("/admin/login.html")
        or path.startswith("/api/admin/login")):
        return await call_next(request)

    # all other /api/* require admin session
    if path.startswith("/api"):
        session = request.cookies.get("admin_session")
        if not session or not verify_session(session):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    # /admin html pages -> redirect when not logged
    if path.startswith("/admin"):
        session = request.cookies.get("admin_session")
        if not session or not verify_session(session):
            return RedirectResponse(url="/admin/login.html")
        return await call_next(request)

    return await call_next(request)

# -------------------------
# Routes
# -------------------------
@app.get("/")
async def root():
    return RedirectResponse(url="/public/index.html", status_code=302)

@app.get("/public/{path:path}")
async def public_files(path: str):
    file_path = os.path.join("public", path)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    resp = FileResponse(file_path)
    if file_path.endswith(".html"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp

@app.get("/admin/login.html", response_class=HTMLResponse)
async def admin_login_page():
    path = os.path.join("public", "admin", "login.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)

@app.get("/api/brand")
async def api_brand():
    return {"name": BUSINESS_NAME, "logo": LOGO_URL}

@app.get("/api/availability")
async def availability(date: str = Query(..., description="YYYY-MM-DD")):
    taken = list_taken_slots_for_date(date)
    pending = list_pending_slots_for_date(date)
    return {"date": date, "taken": taken, "pending": pending,
            "hours": {"open": BUSINESS_HOURS[0], "close": BUSINESS_HOURS[1]}}

@app.post("/api/lead", response_model=LeadResponse)
async def create_lead(lead: Lead):
    taken = list_taken_slots_for_date(lead.appointment_date)
    if lead.appointment_time in taken:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "message": "Selected time is already confirmed.",
                     "booking_status": "conflict", "taken": taken},
        )
    booking_id = write_lead("pending", lead)
    confirm_token = _sign("confirm", booking_id)
    cancel_token  = _sign("cancel", booking_id)
    base = PUBLIC_BASE_URL or ""
    confirm_url = f"{base}/confirm/{booking_id}?token={confirm_token}"
    cancel_url  = f"{base}/cancel/{booking_id}?token={cancel_token}"

    subject, text, html = build_owner_email(booking_id, lead, confirm_url, cancel_url)
    send_via_brevo_api(subject, text, html)

    return {"ok": True, "message": "Lead saved. We will contact you to confirm.",
            "booking_status": "pending", "confirm_url": confirm_url, "cancel_url": cancel_url}

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

    # prevent double-booking same confirmed slot
    for r in leads:
        if (r["booking_id"] != booking_id
            and r["appointment_date"] == target["appointment_date"]
            and r["appointment_time"] == target["appointment_time"]
            and r["status"] == "confirmed"):
            return HTMLResponse("<h2>⚠️ Slot already confirmed for another booking.</h2>", status_code=409)

    if not update_booking_status(booking_id, "confirmed"):
        return HTMLResponse("<h2>Booking not found.</h2>", status_code=404)

    # Send client confirmation with optional -10% payment link
    try:
        to_email = (target.get("email") or "").strip()
        if to_email:
            pay_link = ""
            if PAYMENT_LINK_BASE:
                pay_link = f"{PAYMENT_LINK_BASE}?booking={booking_id}&discount=10&code={PROMO_CODE}"
            subject = "Your booking is confirmed"
            txt = (
                f"Hi {target.get('name')},\n\n"
                f"Your booking for {target.get('service')} on {target.get('appointment_date')} at {target.get('appointment_time')} is confirmed.\n"
            )
            inner = (
                f"<p>Hi {target.get('name')},</p>"
                f"<p>Your booking for <b>{target.get('service')}</b> on <b>{target.get('appointment_date')}</b> at <b>{target.get('appointment_time')}</b> is confirmed.</p>"
            )
            if pay_link:
                txt += f"Optional: pay now with 10% off (code {PROMO_CODE}): {pay_link}\n"
                inner += f"<p><a href='{pay_link}'>Pay now with 10% off (code {PROMO_CODE})</a></p>"
            send_via_brevo_api(subject, txt, _wrap_email_html("Booking Confirmed", inner), to_email=to_email)
    except Exception as e:
        print("Email confirm send failed:", e)

    return HTMLResponse("<h2>✅ Booking confirmed. This slot is now reserved.</h2>")

@app.get("/cancel/{booking_id}", response_class=HTMLResponse)
async def cancel_booking(booking_id: str, token: str):
    if not _verify("cancel", booking_id, token):
        return HTMLResponse("<h2>Invalid or expired cancellation link.</h2>", status_code=403)
    if not update_booking_status(booking_id, "cancelled"):
        return HTMLResponse("<h2>Booking not found.</h2>", status_code=404)
    return HTMLResponse("<h2>🗑️ Booking cancelled. The slot is now free.</h2>")

# ----- Admin-only APIs -----
@app.get("/api/leads")
async def list_leads():
    return {"leads": read_all_leads()}

@app.post("/api/confirm/{booking_id}")
async def api_confirm_booking(booking_id: str):
    leads = read_all_leads()
    target = next((r for r in leads if r["booking_id"] == booking_id), None)
    if not target:
        return JSONResponse({"ok": False, "message": "Booking not found"}, status_code=404)
    if target["status"] == "confirmed":
        return {"ok": True, "message": "Already confirmed"}

    for r in leads:
        if (r["booking_id"] != booking_id
            and r["appointment_date"] == target["appointment_date"]
            and r["appointment_time"] == target["appointment_time"]
            and r["status"] == "confirmed"):
            return JSONResponse({"ok": False, "message": "Time slot already confirmed for another booking."}, status_code=409)

    update_booking_status(booking_id, "confirmed")

    # email client like the public confirm flow
    try:
        to_email = (target.get("email") or "").strip()
        if to_email:
            pay_link = ""
            if PAYMENT_LINK_BASE:
                pay_link = f"{PAYMENT_LINK_BASE}?booking={booking_id}&discount=10&code={PROMO_CODE}"
            subject = "Your booking is confirmed"
            txt = (
                f"Hi {target.get('name')},\n\n"
                f"Your booking for {target.get('service')} on {target.get('appointment_date')} at {target.get('appointment_time')} is confirmed.\n"
            )
            inner = (
                f"<p>Hi {target.get('name')},</p>"
                f"<p>Your booking for <b>{target.get('service')}</b> on <b>{target.get('appointment_date')}</b> at <b>{target.get('appointment_time')}</b> is confirmed.</p>"
            )
            if pay_link:
                txt += f"Optional: pay now with 10% off (code {PROMO_CODE}): {pay_link}\n"
                inner += f"<p><a href='{pay_link}'>Pay now with 10% off (code {PROMO_CODE})</a></p>"
            send_via_brevo_api(subject, txt, _wrap_email_html("Booking Confirmed", inner), to_email=to_email)
    except Exception as e:
        print("Email confirm send failed:", e)

    return {"ok": True, "message": "Booking confirmed"}

@app.post("/api/cancel/{booking_id}")
async def api_cancel_booking_admin(booking_id: str):
    ok = update_booking_status(booking_id, "cancelled")
    if not ok:
        return JSONResponse({"ok": False, "message": "Booking not found"}, status_code=404)
    return {"ok": True, "message": "Booking cancelled"}

@app.post("/api/paid/{booking_id}")
async def api_set_paid(booking_id: str, paid: str = Query("yes")):
    ok = set_paid(booking_id, make_paid=(paid.lower() in ("1","true","yes","paid")))
    if not ok:
        return JSONResponse({"ok": False, "message": "Booking not found"}, status_code=404)
    return {"ok": True, "message": ("Marked as paid" if paid.lower() in ("1","true","yes","paid") else "Marked as unpaid")}

# ----- Debug helpers -----
@app.get("/__routes")
def list_routes():
    return sorted([r.path for r in app.router.routes if isinstance(r, APIRoute)])

# -------------------------
# Chatbot (public)
# -------------------------
DATE_RX = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
TIME_RX = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\b")

def _iso_today(offset_days: int = 0) -> str:
    return (datetime.utcnow().date() + timedelta(days=offset_days)).isoformat()

def _extract_relative_date(text: str) -> Optional[str]:
    low = text.lower()
    if "today" in low:
        return _iso_today(0)
    if "tomorrow" in low or "tmrw" in low:
        return _iso_today(1)
    return None

def _nice_reply(text: str) -> str:
    # optional softening via OpenAI; otherwise just return the text
    if not OPENAI_API_KEY:
        return text
    try:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are a concise, warm booking assistant. Keep replies under 120 words."},
                {"role": "user", "content": text},
            ],
            "temperature": 0.3,
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {OPENAI_API_KEY}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"OpenAI nicening failed: {e}")
        return text

from typing import Dict
@app.post("/api/chat")
async def chat(payload: Dict[str, str]):
    msg = (payload.get("message") or "").strip()
    if not msg:
        return {"reply": _nice_reply("Hi! I can check availability, book a slot, share prices/location, or connect you to a human. What would you like to do?")}

    low = msg.lower()

    # Meta / permission-to-ask
    if any(p in low for p in ["can i ask", "can i ask you", "may i ask", "ask you something", "can i talk", "can i speak"]):
        return {"reply": _nice_reply("Of course — go ahead! I can check availability, make a reservation, tell you about prices or our location, or connect you to a human.")}

    # Greetings
    if any(w in low for w in ["hello", "hi ", "hey", "good morning", "good afternoon", "good evening"]):
        return {"reply": _nice_reply("Hi there! I can check availability, book a slot, share prices/location, or connect you to a human. What can I do for you?")}

    # Pricing / location
    if "price" in low or "cost" in low or "fee" in low:
        return {"reply": _nice_reply("Pricing varies by service. Tell me what you need and I’ll confirm a quote or connect you to a human.")}
    if "where" in low or "address" in low or "location" in low or "office" in low:
        return {"reply": _nice_reply("We’re in Sofia. If you need directions, I can have a human send you details.")}

    # Human
    if "human" in low or "agent" in low or "person" in low or "contact" in low:
        return {"reply": _nice_reply("Sure — say “talk to an agent” and leave your phone. We’ll contact you shortly.")}

    # Availability intent
    if any(k in low for k in ["avail", "availability", "free", "slot", "slots"]):
        date_match = DATE_RX.search(msg)
        rel_date = _extract_relative_date(msg)
        if not (date_match or rel_date):
            return {"reply": _nice_reply("For availability, say “availability today”, “availability tomorrow”, or a date like 2025-10-13.")}
        date_str = (date_match.group(0) if date_match else rel_date)
        taken = list_taken_slots_for_date(date_str)
        pending = list_pending_slots_for_date(date_str)
        if taken or pending:
            return {"reply": _nice_reply(f"On {date_str}, I see these times — confirmed: {taken or 'none'}; pending: {pending or 'none'}. Tell me which time you want and I’ll pencil it in.")}
        return {"reply": _nice_reply(f"On {date_str}, everything looks open between {BUSINESS_HOURS[0]}–{BUSINESS_HOURS[1]}. What time works for you?")}

    # Booking intent (simple guidance)
    if "book" in low or "reserve" in low or "appointment" in low:
        return {"reply": _nice_reply("Tell me: your name, phone, service, date (YYYY-MM-DD) and time (HH:MM). Example: “Book me for consultation tomorrow at 14:30, I'm Alex, phone +359…”.")}

    # Natural fallback
    return {"reply": _nice_reply("I might not have a perfect answer for that. I can help you check availability, make a booking, share prices/location, or connect you to a human. What would you like to do?")}

# -------------------------
# Admin login/logout
# -------------------------
from fastapi import Form
@app.post("/admin/login")
async def admin_login(request: Request):
    username = password = ""
    try:
        data = await request.json()
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
    except Exception:
        pass
    if not username:
        form = await request.form()
        username = (form.get("username") or "").strip()
        password = (form.get("password") or "").strip()

    if username == ADMIN_USER and password == ADMIN_PASS:
        token = create_session(username)
        accept = request.headers.get("accept", "")
        if "application/json" in accept or request.headers.get("x-requested-with"):
            resp = JSONResponse({"ok": True})
        else:
            resp = RedirectResponse(url="/public/admin.html", status_code=302)
        resp.set_cookie("admin_session", token, max_age=60*60*24*7, httponly=True, samesite="None", secure=True, path="/")
        return resp

    accept = request.headers.get("accept", "")
    if "application/json" in accept or request.headers.get("x-requested-with"):
        return JSONResponse({"ok": False, "error": "invalid"}, status_code=401)
    return RedirectResponse(url="/admin/login.html?error=Invalid+credentials", status_code=302)

@app.post("/api/admin/login")
async def admin_login_alias(request: Request):
    return await admin_login(request)

@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin/login.html", status_code=302)
    resp.delete_cookie("admin_session", path="/")
    return resp
