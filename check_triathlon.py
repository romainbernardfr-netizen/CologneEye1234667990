import hashlib
import os
import smtplib
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# --- CONFIGURATION (Loaded from GitHub Repository Secrets) ---
URL = "https://www.koeln-triathlon.com/startplatzboerse/"
STATE_FILE = "last_page_hash.txt"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Optional notification channels (configure one or several in GitHub Secrets)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")


class SlotCheckResult(BaseModel):
    slots_available: bool = Field(
        description="True ONLY if at least one real triathlon starting slot (Startplatz) is currently listed for sale."
    )
    details: str = Field(
        description="Summary of available slots (distance, bib number, contact/link) or explanation if empty."
    )


def get_clean_page_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TriathlonSlotWatcher/1.0)"}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()

    return " ".join(soup.get_text(separator=" ").split())


def has_page_changed(text: str) -> bool:
    current_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            if f.read().strip() == current_hash:
                return False

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(current_hash)
    return True


def analyze_with_gemini(page_text: str) -> SlotCheckResult:
    if not GEMINI_API_KEY:
        raise ValueError("Missing GEMINI_API_KEY environment variable.")

    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=20_000),
    )
    prompt = f"""
    Analyze the text from the Köln Triathlon Startplatzbörse webpage below.
    Determine if any starting slots (Mitteldistanz, Olympische Distanz, Sprintdistanz, etc.) 
    are currently listed for transfer/sale under 'Folgende Startplätze werden aktuell angeboten'.
    Ignore general instructions on how the exchange works.

    Page text:
    {page_text[:15000]}
    """

    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SlotCheckResult,
                thinking_config=types.ThinkingConfig(thinking_level="LOW"),
            ),
        )
        return SlotCheckResult.model_validate_json(response.text)

    except Exception as e:
        print(f"Gemini temporarily unavailable ({e}). Falling back to direct text check...")
        marker = "Folgende Startplätze werden aktuell angeboten:"
        if marker in page_text:
            listing_area = page_text.split(marker, 1)[1].strip()
            if listing_area and not listing_area.startswith("Medien."):
                return SlotCheckResult(slots_available=True, details=listing_area[:500])
        return SlotCheckResult(slots_available=False, details="Fallback check: no slots listed.")


def send_notifications(details: str) -> None:
    sent_any = False
    message_body = f"🚨 Köln Triathlon Slot Available!\n\n{details}\n\nLink: {URL}"

    # 1. ntfy.sh
    if NTFY_TOPIC:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message_body.encode("utf-8"),
            headers={
                "Title": "Köln Triathlon Slot Available!",
                "Priority": "urgent",
                "Tags": "rotating_light,swimmer,bike,runner",
                "Click": URL,
            },
            timeout=10,
        )
        print("Sent notification via ntfy.")
        sent_any = True

    # 2. Telegram Bot
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message_body},
            timeout=10,
        )
        print(f"Telegram response ({resp.status_code}): {resp.text}")
        resp.raise_for_status()
        sent_any = True
    else:
        print(f"Telegram skipped -> TOKEN set: {bool(TELEGRAM_TOKEN)}, CHAT_ID set: {bool(TELEGRAM_CHAT_ID)}")

    # 3. Discord Webhook
    if DISCORD_WEBHOOK_URL:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message_body},
            timeout=10,
        )
        print("Sent notification via Discord.")
        sent_any = True

    # 4. Gmail SMTP
    if EMAIL_SENDER and EMAIL_APP_PASSWORD and EMAIL_RECEIVER:
        msg = EmailMessage()
        msg["Subject"] = "🚨 Köln Triathlon Slot Available!"
        msg["From"] = EMAIL_SENDER
        msg["To"] = EMAIL_RECEIVER
        msg.set_content(message_body)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_APP_PASSWORD)
            smtp.send_message(msg)
        print("Sent notification via Email.")
        sent_any = True

    if not sent_any:
        print("WARNING: Slots found, but no notification secrets were configured!")


def main() -> None:
    page_text = get_clean_page_text(URL)

    # Temporarily disabled for testing so it always runs:
    # if not has_page_changed(page_text):
    #     print("No change detected on page.")
    #     return

    print("Page changed! Asking Gemini to verify...")
    result = analyze_with_gemini(page_text)

    if True:  # Temporarily True for testing — change back to `if result.slots_available:` after your test buzzes!
        print(f"SLOTS FOUND: {result.details}")
        send_notifications(result.details)
    else:
        print(f"Page changed, but no slots listed: {result.details}")


if __name__ == "__main__":
    main()
