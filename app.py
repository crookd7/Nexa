1) IMPORTS
import os
import csv, json, uuid, hmac, hashlib, urllib.request, re
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from fastapi import FastAPI, Request, HTTPException, Query, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from itsdangerous import URLSafeSerializer
# (you had Query twice; one import is enough)

# 2) STRIPE CONFIG + HELPERS (your exact code)
BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5000")
STRIPE_CURRENCY = (os.getenv("STRIPE_CURRENCY") or "eur").lower()
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL") or f"{BASE_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}"
STRIPE_CANCEL_URL  = os.getenv("STRIPE_CANCEL_URL")  or f"{BASE_URL}/payment/cancelled"

def get_stripe():
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    return stripe

def create_checkout_url(amount_cents: int, email: str, description: str, booking_id: str) -> str:
    stripe = get_stripe()
    session = stripe.checkout.Session.create(
        mode="payment",
        customer_email=email,
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": STRIPE_CURRENCY,
                "unit_amount": amount_cents,
                "product_data": {"name": description, "metadata": {"booking_id": booking_id}},
            },
        }],
        metadata={"booking_id": booking_id},
        success_url=STRIPE_SUCCESS_URL,
        cancel_url=STRIPE_CANCEL_URL,
        allow_promotion_codes=False,
    )
    return session.url

# 3) CREATE THE APP (must be BEFORE any @app.get)
app = FastAPI()

# 4) ROUTES (now these see 'app')
@app.get("/dev/create-pay-link", response_class=PlainTextResponse)
async def dev_create_pay_link(
    to_email: str,
    booking_id: str,
    amount_cents: int,
    description: str = "Booking payment – 10% online discount",
):
    try:
        url = create_checkout_url(
            amount_cents=amount_cents,
            email=to_email,
            description=description,
            booking_id=booking_id,
        )
        return url
    except Exception as e:
        print(f"[Stripe] Error: {e}")
        return PlainTextResponse("Failed to create Stripe session. Check logs.", status_code=500)

@app.get("/test")
async def test():
    return {"ok": True}

@app.get("/__routes", response_class=PlainTextResponse)
async def list_routes():
    lines = []
    for r in app.router.routes:
        try:
            methods = ",".join(sorted(r.methods))
        except Exception:
            methods = ""
        lines.append(f"{methods:8}  {getattr(r, 'path', getattr(r, 'path_format', ''))}")
    return "\n".join(sorted(lines))

# -------------------------
# Environment / Config
# -------------------------
NEXA_SERVER_KEY = (os.getenv("NEXA_SERVER_KEY") or "").strip()

# Brevo (HTTP API)
BREVO_API_KEY = (os.getenv("BREVO_API_KEY") or "").strip()
SMTP_FROM = (os.getenv("SMTP_FROM") or "").strip()
NOTIFY_TO = (os.getenv("NOTIFY_TO") or "").strip()

# Email links signing
ADMIN_SECRET = (os.getenv("ADMIN_SECRET") or "").strip()
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip()

# Admin session
ADMIN_USER = (os.getenv("ADMIN_USER") or "admin").strip()
ADMIN_PASS = (os.getenv("ADMIN_PASS") or "changeme").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET") or "supersecret123"
serializer = URLSafeSerializer(SESSION_SECRET, salt="admin-session")

# Optional – nicer wording for replies
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()

# Business copy for FAQs
BUSINESS_DESC = (os.getenv("BUSINESS_DESC") or
                 "We provide consultations and scheduling for clients in Sofia.").strip()

# Data (ephemeral unless using a Render Disk)
LEADS_FILE = os.getenv("LEADS_FILE") or "leads.csv"
CSV_HEADER = [
    "booking_id", "timestamp_utc", "status", "name", "email", "phone",
    "service", "appointment_date", "appointment_time"
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
    if not os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)
        print(f"📄 Created CSV {LEADS_FILE}")

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
        rd = csv.reader(f)
        _ = next(rd, None)
        for row in rd:
            if not row or len(row) < len(CSV_HEADER):
                continue
            out.append(_row_to_dict(row))
    print(f"📖 Loaded {len(out)} leads from CSV")
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
    print(f"📝 Wrote lead {booking_id} {lead.appointment_date} {lead.appointment_time} [{status}]")
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
    print(f"🔁 Updated {booking_id} -> {new_status}")
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
    if not BREVO_API_KEY or not (SMTP_FROM and NOTIFY_TO):
        return
    payload = {
        "sender": {"email": SMTP_FROM, "name": (os.getenv("BUSINESS_NAME") or "Nexa")},
        "to": [{"email": (to_email or NOTIFY_TO)}],
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
            print(f"✅ Brevo email sent: {resp.status}")
    except Exception as e:
        print(f"❌ Brevo email failed: {e}")

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
      <div style="margin-top:16px">
        <a href="{confirm_url}" style="display:inline-block;background:#16a34a;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:700;margin-right:8px">✓ Confirm</a>
        <a href="{cancel_url}" style="display:inline-block;background:#ef4444;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:700">✕ Cancel</a>
      </div>
    </div>
    """
    return subject, text, html

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
# Middleware (final version)
# -------------------------
@app.middleware("http")
async def protect(request: Request, call_next):
    path = request.url.path

    # ---- public endpoints (no auth) ----
    if (
        path.startswith("/api/availability")
        or path.startswith("/api/chat")
        or path.startswith("/api/chat-contact")
    ):
        return await call_next(request)

    # ---- public lead submit ONLY for /api/lead (NOT /api/leads) ----
    if path == "/api/lead" or path.startswith("/api/lead/"):
        header_key = request.headers.get("X-Nexa-Key", "")
        if NEXA_SERVER_KEY and header_key != NEXA_SERVER_KEY:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    # ---- admin login page & login POST are public ----
    if path.startswith("/admin/login") or path.endswith("/admin/login.html"):
        return await call_next(request)

    # ---- all other /api/* require admin session ----
    if path.startswith("/api"):
        session = request.cookies.get("admin_session")
        if not session or not verify_session(session):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    # ---- /admin HTML pages redirect to login when no session ----
    if path.startswith("/admin"):
        session = request.cookies.get("admin_session")
        if not session or not verify_session(session):
            return RedirectResponse(url="/admin/login.html")
        return await call_next(request)

    # everything else
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
    if file_path.endswith('.html'):
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
    return resp

@app.get("/admin/login.html", response_class=HTMLResponse)
async def admin_login_page():
    path = os.path.join("public", "admin", "login.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)

@app.get("/api/availability")
async def availability(date: str = Query(..., description="YYYY-MM-DD")):
    taken = list_taken_slots_for_date(date)
    pending = list_pending_slots_for_date(date)
    return {
        "date": date,
        "taken": taken,
        "pending": pending,
        "hours": {"open": BUSINESS_HOURS[0], "close": BUSINESS_HOURS[1]},
    }

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
        "confirm_url": confirm_url,
        "cancel_url": cancel_url,
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

    for r in leads:
        if (
            r["booking_id"] != booking_id
            and r["appointment_date"] == target["appointment_date"]
            and r["appointment_time"] == target["appointment_time"]
            and r["status"] == "confirmed"
        ):
            return HTMLResponse("<h2>⚠️ Slot already confirmed for another booking.</h2>", status_code=409)

    if not update_booking_status(booking_id, "confirmed"):
        return HTMLResponse("<h2>Booking not found.</h2>", status_code=404)
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


# ----- Debug helpers -----
@app.post("/api/debug/create_dummy")
async def create_dummy():
    today = datetime.utcnow().date().isoformat()
    now_hhmm = (datetime.utcnow() + timedelta(minutes=5)).strftime("%H:%M")
    lead = Lead(
        name="Test Lead",
        email="",
        phone="+359000000000",
        service="test",
        appointment_date=today,
        appointment_time=now_hhmm
    )
    booking_id = write_lead("pending", lead)
    return {"ok": True, "booking_id": booking_id, "date": today, "time": now_hhmm}

@app.get("/api/debug/whoami")
async def debug_whoami(request: Request):
    tok = request.cookies.get("admin_session")
    return {"has_cookie": bool(tok), "valid_session": bool(tok and verify_session(tok))}

@app.get("/api/debug/leads")
async def debug_leads():
    leads = read_all_leads()
    return {"count": len(leads), "sample": leads[:5]}

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
    if not OPENAI_API_KEY:
        return text
    try:
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": "You are a concise, warm booking assistant. Keep replies under 120 words."},
                {"role": "user", "content": text},
            ],
            "temperature": 0.2,
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

@app.post("/api/chat")
async def chat(payload: Dict[str, str]):
    msg = (payload.get("message") or "").strip()
    if not msg:
        return {"reply": "Hey! I can check availability, pencil you in, or answer quick questions. Try: ‘availability today’ or ‘book me tomorrow at 10:00’."}

    low = msg.lower()

    # FAQ / small talk
    if any(w in low for w in ["hello", "hi ", "hey", "good morning", "good afternoon", "good evening"]):
        return {"reply": _nice_reply("Hi there! 👋 I can check availability, help you book, or answer quick questions. What can I do for you today?")}
    if "what kind of business" in low or "who are you" in low or "what is this" in low or "what do you do" in low:
        return {"reply": _nice_reply(BUSINESS_DESC)}
    if any(k in low for k in ["hour", "open", "close", "working"]):
        return {"reply": _nice_reply("We’re open from 09:00 to 18:00, Monday to Friday.")}
    if any(k in low for k in ["where", "address", "location", "office"]):
        return {"reply": _nice_reply("We’re in Sofia. If you need directions, I can have a human text you details.")}
    if "service" in low or "offer" in low:
        return {"reply": _nice_reply("We offer consultations and scheduling. Tell me what you need and I’ll help book a slot.")}
    if "price" in low or "cost" in low or "fee" in low:
        return {"reply": _nice_reply("Pricing varies by service. I can connect you with a human to confirm a quote.")}
    if "human" in low or "agent" in low or "person" in low or "contact" in low:
        return {"reply": _nice_reply("Absolutely—tap “Talk to an agent” and leave your phone. We’ll call you shortly.")}

    # Availability
    date_match = DATE_RX.search(msg)
    rel_date = _extract_relative_date(msg)
    if any(k in low for k in ["avail", "free", "slot", "slots"]):
        if not (date_match or rel_date):
            base = f"Our hours are {BUSINESS_HOURS[0]}–{BUSINESS_HOURS[1]}, Mon–Fri. Say ‘availability today’, ‘availability tomorrow’, or a date like 2025-10-05."
            return {"reply": _nice_reply(base)}
        date_str = date_match.group(1) if date_match else rel_date
        taken = list_taken_slots_for_date(date_str)
        pending = list_pending_slots_for_date(date_str)
        if not taken and not pending:
            base = f"{date_str}: All times look open between {BUSINESS_HOURS[0]} and {BUSINESS_HOURS[1]}."
        else:
            t = ", ".join(taken) if taken else "none"
            p = ", ".join(pending) if pending else "none"
            base = f"{date_str} — Confirmed (blocked): {t}. Pending: {p}. Tell me a time and I can tentatively book you."
        return {"reply": _nice_reply(base)}

    # Booking
    time_rx = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\b")
    if "book" in low or "schedule" in low or "appointment" in low:
        date_m = DATE_RX.search(msg)
        if not date_m:
            rel = _extract_relative_date(msg)
            if not rel:
                return {"reply": _nice_reply("Please include a date (YYYY-MM-DD), e.g. ‘book me for a consultation on 2025-10-05 at 14:30’.")}
            date_str = rel
        else:
            date_str = date_m.group(1)

        time_m = time_rx.search(msg)
        if not time_m:
            return {"reply": _nice_reply("Please include a time (HH:MM), e.g. 14:30.")}

        time_str = f"{time_m.group(1)}:{time_m.group(2)}"
        name_m = re.search(r"(?:i am|i'm|name is)\s+([^\.,\n]+)", low) or re.search(r"\bname\s*:\s*([^\.,\n]+)", low)
        phone_m = re.search(r"(?:phone|tel|mobile|gsm)\s*[:\-]?\s*([\+\d][\d\s\-]{6,})", low)
        service_m = re.search(r"(?:service|for|need|want)\s+([a-zA-Zа-яА-Я0-9 \-_/]{2,})", msg)

        name = (name_m.group(1).strip() if name_m else "Guest").title()
        phone = (phone_m.group(1).strip() if phone_m else "unknown")
        service = (service_m.group(1).strip() if service_m else "service")

        taken = list_taken_slots_for_date(date_str)
        if time_str in taken:
            return {"reply": _nice_reply(f"That time ({date_str} {time_str}) is already confirmed. Try another time.")}

        lead = Lead(
            name=name, email=None, phone=phone, service=service,
            appointment_date=date_str, appointment_time=time_str
        )
        booking_id = write_lead("pending", lead)

        confirm_token = _sign("confirm", booking_id)
        cancel_token = _sign("cancel", booking_id)
        base_url = PUBLIC_BASE_URL or ""
        confirm_url = f"{base_url}/confirm/{booking_id}?token={confirm_token}"
        cancel_url = f"{base_url}/cancel/{booking_id}?token={cancel_token}"
        subject, text, html = build_owner_email(booking_id, lead, confirm_url, cancel_url)
        send_via_brevo_api(subject, text, html)

        base = f"Done! I created a pending booking for {name} on {date_str} at {time_str} for ‘{service}’. The owner will confirm shortly."
        return {"reply": _nice_reply(base)}

    help_text = (
        "I can check availability or tentatively book you.\n"
        "• availability today / tomorrow\n"
        "• availability 2025-10-05\n"
        "• book me for consultation tomorrow at 14:30, I'm Alex, phone +359…\n"
        "You can also say “talk to an agent”."
    )
    return {"reply": _nice_reply(help_text)}


@app.post("/api/confirm/{booking_id}")
async def api_confirm_booking(booking_id: str):
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

    try:
        to_email = (target.get("email") or "").strip()
        if to_email:
            promo = os.getenv("PROMO_CODE") or "NEXA10"
            pay_link = (os.getenv("PAYMENT_LINK_BASE") or "").strip()
            if pay_link:
                pay_link = f"{pay_link}?booking={booking_id}&discount=10&code={promo}"

            subject = "Your booking is confirmed"
            txt = (
                f"Hi {target.get('name')},\n\n"
                f"Your booking for {target.get('service')} on {target.get('appointment_date')} at {target.get('appointment_time')} is confirmed.\n"
            )
            if pay_link:
                txt += f"Optional: pay now with 10% off using code {promo}: {pay_link}\n"

            inner = (
                f"<p>Hi {target.get('name')},</p>"
                f"<p>Your booking for <b>{target.get('service')}</b> on <b>{target.get('appointment_date')}</b> at <b>{target.get('appointment_time')}</b> is confirmed.</p>"
                + (f"<p><a href='{pay_link}'>Pay now with 10% off (code {promo})</a></p>" if pay_link else "")
            )
            try:
                html = _wrap_email_html("Booking Confirmed", inner)  # type: ignore
            except Exception:
                html = inner

            send_via_brevo_api(subject, txt, html, to_email=to_email)
    except Exception as e:
        print("Email confirm send failed:", e)

    return {"ok": True, "message": "Booking confirmed"}



@app.post("/api/cancel/{booking_id}")
async def api_cancel_booking(booking_id: str):
    ok = update_booking_status(booking_id, "cancelled")
    if not ok:
        return JSONResponse({"ok": False, "message": "Booking not found"}, status_code=404)

    try:
        leads = read_all_leads()
        target = next((r for r in leads if r["booking_id"] == booking_id), None)
        to_email = (target.get("email") or "").strip() if target else ""
        if to_email:
            subject = "Your booking was cancelled"
            txt = (
                f"Hi {target.get('name')},\n\n"
                f"Your booking for {target.get('service')} on {target.get('appointment_date')} at {target.get('appointment_time')} was cancelled.\n"
                "If this is unexpected, reply to this email."
            )
            inner = (
                f"<p>Hi {target.get('name')},</p>"
                f"<p>Your booking for <b>{target.get('service')}</b> on <b>{target.get('appointment_date')}</b> at <b>{target.get('appointment_time')}</b> was cancelled.</p>"
                "<p>If this is unexpected, reply to this email.</p>"
            )
            try:
                html = _wrap_email_html("Booking Cancelled", inner)  # type: ignore
            except Exception:
                html = inner

            send_via_brevo_api(subject, txt, html, to_email=to_email)
    except Exception as e:
        print("Email cancel send failed:", e)

    return {"ok": True, "message": "Booking cancelled"}


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