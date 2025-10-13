# --- Chat endpoint (safe strings, professional replies) ---
from typing import Dict  # ensure this import exists

@app.post("/api/chat")
async def chat(payload: Dict[str, str]):
    msg = (payload.get("message") or "").strip()
    if not msg:
        return {"reply": _nice_reply("Hi! I can check availability, book a slot, share prices/location, or connect you to a human. What do you need?")}

    low = msg.lower()

    # Meta / permission to ask
    if any(p in low for p in [
        "can i ask", "can i ask you", "may i ask", "ask you something",
        "can i talk", "can i speak"
    ]):
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
        date_match = DATE_RX.search(msg) if 'DATE_RX' in globals() else None
        rel_date = _extract_relative_date(msg) if '_extract_relative_date' in globals() else None
        if not (date_match or rel_date):
            return {"reply": _nice_reply("For availability, please say a date like “availability today”, “availability tomorrow”, or “availability 2025-10-13”.")}
        date_str = (date_match.group(0) if date_match else rel_date)
        if not date_str:
            return {"reply": _nice_reply("Could you confirm the date? For example: “availability tomorrow” or “availability 2025-10-13”.")}
        taken = list_taken_slots_for_date(date_str) if 'list_taken_slots_for_date' in globals() else []
        pending = list_pending_slots_for_date(date_str) if 'list_pending_slots_for_date' in globals() else []
        if taken or pending:
            return {"reply": _nice_reply(f"On {date_str}, I see these times — taken: {taken} / pending: {pending}. Tell me which time you want and I’ll pencil it in.")}
        return {"reply": _nice_reply(f"On {date_str}, everything looks open between {BUSINESS_HOURS[0]}–{BUSINESS_HOURS[1]}. What time works for you?")}

    # Booking intent (simple guidance)
    if "book" in low or "reserve" in low or "appointment" in low:
        return {"reply": _nice_reply("Tell me: your name, phone, service, date (YYYY-MM-DD) and time (HH:MM). Example: “Book me for consultation tomorrow at 14:30, I'm Alex, phone +359…”.")}

    # Final fallback — triple-quoted to avoid unterminated strings
    help_text = """I can help with:
• availability today / tomorrow
• availability 2025-10-13
• book me for consultation tomorrow at 14:30, I'm Alex, phone +359...
You can also say "talk to an agent"."""
    return {"reply": _nice_reply(
        "I can't help with that directly, but I can help you book, check availability, prices, or location — or connect you to a human.\n\n" + help_text
    )}
