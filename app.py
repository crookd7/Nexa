import os, csv, smtplib, ssl
from typing import List, Dict, Optional
from email.message import EmailMessage
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI

# ------------------ ENV ------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
CHAT_ENABLED = client is not None

# SMTP / Email
SMTP_HOST  = os.getenv("SMTP_HOST")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER")
SMTP_PASS  = os.getenv("SMTP_PASS")
SMTP_FROM  = os.getenv("SMTP_FROM") or (SMTP_USER or "")
NOTIFY_TO  = os.getenv("NOTIFY_TO")

# API auth
NEXA_SERVER_KEY = os.getenv("NEXA_SERVER_KEY")  # set in Render
ALLOWED_ORIGINS = ["https://nexa-p6nu.onrender.com"]  # add your custom domain later

LEADS_CSV = "leads.csv"

# ------------------ APP ------------------
app = FastAPI(title="Nexa LeadGenBot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "X-Nexa-Key"],
)

app.mount("/public", StaticFiles(directory="public", html=True), name="public")

@app.get("/")
def root():
    return RedirectResponse(url="/public/index.html")

@app.get("/health")
def health():
    return {"ok": True}

# ------------------ MODELS ------------------
class ChatRequest(BaseModel):
    message: str
    history: List[Dict[str, str]] = []

class ChatResponse(BaseModel):
    reply: str

class Lead(BaseModel):
    name: str
    phone: str
    service: Optional[str] = None
    preferred_time: Optional[str] = None
    appointment_date: Optional[str] = None  # YYYY-MM-DD
    appointment_time: Optional[str] = None  # HH:MM

# ------------------ GUARD (checked per-request) ------------------
def guard(x_nexa_key: Optional[str]):
    if not NEXA_SERVER_KEY:
        raise HTTPException(status_code=500, detail="Server key not configured")
    if x_nexa_key != NEXA_SERVER_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ------------------ CHAT ------------------
@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, x_nexa_key: Optional[str] = Header(None)):
    guard(x_nexa_key)  # check header at request time
    if not CHAT_ENABLED:
        return ChatResponse(
            reply="Nexa chat is temporarily unavailable. You can still submit the form and we’ll contact you ASAP."
        )
    system = (
        "You are Nexa, a friendly lead-generation assistant for a local business. "
        "Introduce yourself as Nexa. Ask for name, phone, service, and a specific date/time. "
        "Keep replies under 2 sentences."
    )
    messages = [{"role": "system", "content": system}] + req.history + [
        {"role": "user", "content": req.message}
    ]
    try:
        completion = client.chat.completions.create(
            model=MODEL, messages=messages, temperature=0.2
        )
        return ChatResponse(reply=completion.choices[0].message.content)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

# ------------------ EMAIL ------------------
def send_lead_email(lead: Lead):
    """Send HTML email via Brevo with TLS."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and NOTIFY_TO):
        print("⚠ Email settings not configured; skipping email send.")
        return

    msg = EmailMessage()
    subj = f"🟢 New lead: {lead.name}"
    if lead.service: subj += f" — {lead.service}"
    if lead.appointment_date and lead.appointment_time:
        subj += f" ({lead.appointment_date} {lead.appointment_time})"
    msg["Subject"] = subj
    msg["From"] = SMTP_FROM
    msg["To"] = NOTIFY_TO

    # text
    lines = [
        "New lead from Nexa",
        f"Name:\t{lead.name}",
        f"Phone:\t{lead.phone}",
        f"Service:\t{lead.service or '-'}",
        f"Preferred time:\t{(lead.appointment_date or '')} {(lead.appointment_time or '')}".strip(),
        "Sent by Nexa Lead Assistant",
    ]
    msg.set_content("\n".join(lines))

    # html
    html = f"""
    <html><body style="font-family:Arial, sans-serif; background:#f9fafb; padding:16px;">
      <table style="max-width:520px;margin:auto;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;">
        <tr><td style="font-size:18px;font-weight:700;color:#004aad;">📩 New Lead from Nexa</td></tr>
        <tr><td style="padding-top:10px;">
          <p><b>Name:</b> {lead.name}</p>
          <p><b>Phone:</b> <a href="tel:{lead.phone}" style="color:#004aad">{lead.phone}</a></p>
          {f"<p><b>Service:</b> {lead.service}</p>" if lead.service else ""}
          {f"<p><b>Preferred Time:</b> {lead.appointment_date or ''} {lead.appointment_time or ''}</p>" if (lead.appointment_date or lead.appointment_time) else ""}
        </td></tr>
        <tr><td style="font-size:12px;color:#6b7280;padding-top:12px;">Sent by Nexa Lead Assistant</td></tr>
      </table>
    </body></html>
    """
    msg.add_alternative(html, subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
            print("✅ Lead email sent successfully.")
    except Exception as e:
        print(f"⚠ Email send failed: {e}")

# ------------------ SAVE LEAD ------------------
@app.post("/api/lead")
def save_lead(lead: Lead, x_nexa_key: Optional[str] = Header(None)):
    guard(x_nexa_key)  # check header at request time
    file_exists = os.path.exists(LEADS_CSV)
    try:
        with open(LEADS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not file_exists:
                w.writerow([
                    "name", "phone", "service",
                    "preferred_time", "appointment_date", "appointment_time"
                ])
            w.writerow([
                lead.name,
                lead.phone,
                lead.service or "",
                lead.preferred_time or "",
                lead.appointment_date or "",
                lead.appointment_time or ""
            ])
        send_lead_email(lead)
        return {"ok": True, "message": "Lead saved."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed: {e}")

# ------------------ TEST EMAIL (optional) ------------------
@app.get("/api/test-email")
def test_email(x_nexa_key: Optional[str] = Header(None)):
    guard(x_nexa_key)
    try:
        dummy = Lead(
            name="Test User",
            phone="+359888000111",
            service="Test Service",
            appointment_date=datetime.now().strftime("%Y-%m-%d"),
            appointment_time="15:00",
        )
        send_lead_email(dummy)
        return JSONResponse(content={"ok": True, "message": "Test email sent."})
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
