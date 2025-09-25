import os, csv, smtplib
from typing import List, Dict, Optional
from email.message import EmailMessage
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
from openai import OpenAI

# ------------------ ENV & CLIENTS ------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing")

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
client = OpenAI(api_key=OPENAI_API_KEY)

# SMTP / Email
SMTP_HOST  = os.getenv("SMTP_HOST")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER")
SMTP_PASS  = os.getenv("SMTP_PASS")
SMTP_FROM  = os.getenv("SMTP_FROM", SMTP_USER or "")
NOTIFY_TO  = os.getenv("NOTIFY_TO")

LEADS_CSV = "leads.csv"

# ------------------ FASTAPI APP ------------------
app = FastAPI(title="Nexa LeadGenBot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.mount("/public", StaticFiles(directory="public", html=True), name="public")
from fastapi.responses import RedirectResponse

@app.get("/")
def root():
    return RedirectResponse(url="/public/index.html")

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

# ------------------ CHAT ------------------
@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
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

# ------------------ EMAIL + ICS ------------------
def send_lead_email(lead: Lead):
    """Sends an HTML email; attaches .ics if appointment_date & time provided."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and NOTIFY_TO):
        print("⚠ Email settings not configured; skipping email send.")
        return

    msg = EmailMessage()

    # Subject like: "🟢 New lead: John — Haircut (2025-09-25 15:00)"
    subject_parts = [f"🟢 New lead: {lead.name}"]
    if lead.service:
        subject_parts.append(f"— {lead.service}")
    if lead.appointment_date and lead.appointment_time:
        subject_parts.append(f"({lead.appointment_date} {lead.appointment_time})")
    msg["Subject"] = " ".join(subject_parts)
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = NOTIFY_TO

    # Plain text fallback
    plain = ["New lead from Nexa", f"Name:\t{lead.name}", f"Phone:\t{lead.phone}"]
    if lead.service:
        plain.append(f"Service:\t{lead.service}")
    if lead.appointment_date or lead.appointment_time:
        plain.append(f"Preferred time:\t{(lead.appointment_date or '')} {(lead.appointment_time or '')}")
    plain.append("Sent by Nexa Lead Assistant")
    msg.set_content("\n".join(plain))

    # HTML body
    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif; background:#f9fafb; padding:20px;">
      <table style="max-width:520px;margin:auto;background:#ffffff;border-radius:10px;padding:20px;border:1px solid #e5e7eb;">
        <tr><td style="font-size:18px;font-weight:bold;color:#004aad;">📩 New Lead from Nexa</td></tr>
        <tr><td style="padding-top:10px;">
          <p><b>Name:</b> {lead.name}</p>
          <p><b>Phone:</b> <a href="tel:{lead.phone}" style="color:#004aad;text-decoration:none;">{lead.phone}</a></p>
          {f"<p><b>Service:</b> {lead.service}</p>" if lead.service else ""}
          {f"<p><b>Preferred Time:</b> {lead.appointment_date or ''} {lead.appointment_time or ''}</p>" if (lead.appointment_date or lead.appointment_time) else ""}
        </td></tr>
        <tr><td style="font-size:12px;color:#6b7280;padding-top:15px;">Sent by Nexa Lead Assistant</td></tr>
      </table>
    </body>
    </html>
    """
    msg.add_alternative(html, subtype="html")

    # Optional: attach calendar invite (1 hour duration) if date/time provided
    if lead.appointment_date and lead.appointment_time:
        try:
            start_dt = datetime.strptime(
                f"{lead.appointment_date} {lead.appointment_time}", "%Y-%m-%d %H:%M"
            )
            end_dt = start_dt + timedelta(hours=1)
            ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//NexaBot//LeadGen//EN
METHOD:REQUEST
BEGIN:VEVENT
UID:{int(datetime.now().timestamp())}@nexa
DTSTAMP:{datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")}
DTSTART:{start_dt.strftime("%Y%m%dT%H%M%S")}
DTEND:{end_dt.strftime("%Y%m%dT%H%M%S")}
SUMMARY:Appointment with {lead.name}
DESCRIPTION:Service: {lead.service or 'N/A'}\\nPhone: {lead.phone}
END:VEVENT
END:VCALENDAR
"""
            msg.add_attachment(
                ics.encode("utf-8"),
                maintype="text",
                subtype="calendar",
                filename="nexa-appointment.ics",
                disposition="attachment",
                params={"method": "REQUEST", "name": "nexa-appointment.ics"},
            )
        except Exception as e:
            print(f"⚠ Failed to generate calendar invite: {e}")

    # Send
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
            print("✅ Lead email sent successfully.")
    except Exception as e:
        print(f"⚠ Email send failed: {e}")

# ------------------ SAVE LEAD ------------------
@app.post("/api/lead")
def save_lead(lead: Lead):
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

        # email notification
        send_lead_email(lead)
        return {"ok": True, "message": "Lead saved."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed: {e}")

# ------------------ TEST EMAIL ------------------
@app.get("/api/test-email")
def test_email():
    dummy = Lead(
        name="Test User",
        phone="+359888000111",
        service="Test Service",
        appointment_date=datetime.now().strftime("%Y-%m-%d"),
        appointment_time="15:00",
    )
    try:
        send_lead_email(dummy)
        return {"ok": True, "message": "Test email sent."}
    except Exception as e:
        return {"ok": False, "error": str(e)}
