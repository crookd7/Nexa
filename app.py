import os
import csv
import json
import urllib.request
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
NEXA_SERVER_KEY = os.getenv("NEXA_SERVER_KEY")
BREVO_API_KEY = os.getenv("BREVO_API_KEY")
SMTP_FROM = os.getenv("SMTP_FROM")
NOTIFY_TO = os.getenv("NOTIFY_TO")

LEADS_FILE = "leads.csv"

# -------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # adjust for security later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------------------
# Models
# -------------------------------------------------------------------
class Lead(BaseModel):
    name: str
    email: str
    phone: str
    service: str
    appointment_date: str
    appointment_time: str

# -------------------------------------------------------------------
# Utility: send via Brevo API
# -------------------------------------------------------------------
def send_via_brevo_api(subject, text):
    if not BREVO_API_KEY:
        print("❌ BREVO_API_KEY missing, cannot send email")
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
            "api-key": BREVO_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(f"✅ Brevo API email sent, status {resp.status}")
    except Exception as e:
        print(f"❌ Brevo API email failed: {e}")

# -------------------------------------------------------------------
# Middleware to guard with API key
# -------------------------------------------------------------------
@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.startswith("/api"):
        header_key = request.headers.get("X-Nexa-Key")
        if not NEXA_SERVER_KEY or header_key != NEXA_SERVER_KEY:
            return JSONResponse(
                {"error": "Unauthorized"}, status_code=401
            )
    return await call_next(request)

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------
@app.get("/")
async def root():
    return {"message": "API running."}

@app.get("/public/{path:path}")
async def public_files(path: str):
    file_path = os.path.join("public", path)
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)

@app.post("/api/lead")
async def create_lead(lead: Lead):
    # Save to CSV
    new_file = not os.path.exists(LEADS_FILE)
    with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(
                ["timestamp", "name", "email", "phone", "service", "date", "time"]
            )
        writer.writerow(
            [
                datetime.utcnow().isoformat(),
                lead.name,
                lead.email,
                lead.phone,
                lead.service,
                lead.appointment_date,
                lead.appointment_time,
            ]
        )

    # Build email body
    subject = "New Website Lead"
    body = (
        f"Name: {lead.name}\n"
        f"Email: {lead.email}\n"
        f"Phone: {lead.phone}\n"
        f"Service: {lead.service}\n"
        f"Date: {lead.appointment_date}\n"
        f"Time: {lead.appointment_time}"
    )
    send_via_brevo_api(subject, body)

    return {"ok": True, "message": "Lead saved."}

@app.get("/api/test-email")
async def test_email():
    subject = "Test Email from API"
    body = "This is a test email from your Render FastAPI service."
    send_via_brevo_api(subject, body)
    return {"ok": True, "message": "Test email sent."}
