#!/usr/bin/env python3
"""
GrainOtch Auto-Registration Bot
Fully automatic: OTP Doctor se number leta hai, OTP khud submit karta hai,
aur voucher (2nd SMS) save karke bot mein bhejta hai.
"""

import os
import re
import io
import csv
import json
import random
import asyncio
import logging
from pathlib import Path
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from sms_apis.otpdoctor import OTPDoctorAPI, extract_otp, extract_voucher

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Conversation states ──────────────────────────────────────────────────────
CODES = 0

# ── Config ───────────────────────────────────────────────────────────────────
INDIAN_MALE_NAMES = [
    "Aarav", "Aditya", "Akash", "Anand", "Ankit", "Arjun", "Arnav", "Ashish",
    "Ayaan", "Ayush", "Bhuvan", "Chirag", "Daksh", "Deepak", "Dev", "Dhruv",
    "Farhan", "Gaurav", "Harsh", "Himanshu", "Ishan", "Jai", "Jayesh", "Kabir",
    "Karan", "Kartik", "Krish", "Kunal", "Lakshya", "Manav", "Manish", "Mayank",
    "Mihir", "Mohit", "Nakul", "Neel", "Nikhil", "Nilesh", "Nishant", "Om",
    "Pankaj", "Parth", "Pranav", "Prashant", "Prateek", "Praveen", "Pulkit",
    "Rahul", "Raj", "Rajat", "Rajesh", "Rakesh", "Raman", "Ramesh", "Raunak",
    "Ravi", "Rishabh", "Ritesh", "Rohan", "Rohit", "Sachin", "Sahil", "Saksham",
    "Samir", "Sanjay", "Saurabh", "Shantanu", "Shivam", "Shubham", "Siddharth",
    "Soham", "Sudhir", "Sumit", "Suraj", "Suyash", "Tanmay", "Tarun", "Tushar",
    "Uday", "Vaibhav", "Vijay", "Vikas", "Vikram", "Vinay", "Viraj", "Vishal",
    "Vivek", "Yash", "Yuvraj", "Zaid", "Aakash", "Abhishek", "Amar", "Amitabh",
    "Aniket", "Anubhav", "Ashwin", "Atharv",
]

CITIES = ["Amaravati", "Beed", "Bhandara", "Buldhana"]

BASE_URL = "https://grainotch.theofferclub.in"
DB_FILE  = Path(__file__).parent / "codes_db.json"
_db_lock = asyncio.Lock()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/home/register",
}

# Global OTP Doctor client
_otp_doctor: OTPDoctorAPI | None = None
_grainotch_service_id: str | None = None

# Service name keywords (user confirmed: "grainotch multisms")
SERVICE_KEYWORD = "grainotch"
SERVER_KEYWORD  = "multisms"

# Hardcoded fallback — confirmed live on 2026-06-26
# 13854 = Grainotch | MultiSms 🇮🇳1 | ₹5.5
# 13855 = Grainotch | MultiSms 🇮🇳3 | ₹5.5
GRAINOTCH_SERVICE_ID_FALLBACK = "13854"


def get_otp_doctor() -> OTPDoctorAPI:
    global _otp_doctor
    if _otp_doctor is None:
        key = os.environ.get("OTPDOCTOR_API_KEY", "")
        if not key:
            raise ValueError("OTPDOCTOR_API_KEY nahi mila!")
        _otp_doctor = OTPDoctorAPI(key)
    return _otp_doctor


async def get_grainotch_service_id() -> str:
    """
    Grainotch service ID dhundho — cached after first call.
    Falls back to hardcoded ID 13854 if API lookup fails.
    """
    global _grainotch_service_id
    if _grainotch_service_id:
        return _grainotch_service_id

    loop = asyncio.get_event_loop()
    api  = get_otp_doctor()

    def _find():
        return api.find_service_id(SERVICE_KEYWORD, SERVER_KEYWORD)

    # Try API lookup first (1 attempt — don't block startup)
    try:
        sid = await loop.run_in_executor(None, _find)
        if sid:
            _grainotch_service_id = sid
            logger.info("Grainotch service ID (API): %s", sid)
            return sid
    except Exception as e:
        logger.warning("Service ID API lookup failed: %s", e)

    # Fallback to hardcoded known-good ID
    logger.warning("Using hardcoded fallback service ID: %s", GRAINOTCH_SERVICE_ID_FALLBACK)
    _grainotch_service_id = GRAINOTCH_SERVICE_ID_FALLBACK
    return GRAINOTCH_SERVICE_ID_FALLBACK


async def _prefetch_service_id(app) -> None:
    """Called at bot startup — pre-fetch and cache service ID."""
    logger.info("Pre-fetching Grainotch service ID at startup...")
    sid = await get_grainotch_service_id()
    if sid:
        logger.info("Startup: Grainotch service ID cached = %s", sid)
    else:
        logger.warning("Startup: Grainotch service ID not found. Use /setservice to set manually.")


# ── Database helpers ─────────────────────────────────────────────────────────

def load_db() -> dict:
    if DB_FILE.exists():
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_db(db: dict) -> None:
    tmp = DB_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    tmp.replace(DB_FILE)


async def upsert_code(code: str, status: str, **extra) -> None:
    async with _db_lock:
        db = load_db()
        record = db.get(code, {"code": code, "added_at": datetime.now().isoformat()})
        record["status"] = status
        record.update(extra)
        record["updated_at"] = datetime.now().isoformat()
        db[code] = record
        save_db(db)


# ── GrainOtch website helpers ────────────────────────────────────────────────

def _get_register_page():
    session = requests.Session()
    r = session.get(f"{BASE_URL}/home/register", headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    token_input = soup.find("input", {"name": "token"})
    token = token_input["value"] if token_input else ""
    return session, token


def _request_otp(session, token: str, code: str, mobile: str) -> dict:
    payload = {"phone": mobile, "ccode": code}
    ajax_headers = {
        **HEADERS,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": BASE_URL,
    }
    r = session.post(
        f"{BASE_URL}/home/generateOTP",
        data=payload,
        headers=ajax_headers,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "success":
        return {"success": True, "msg": "OTP sent"}
    msg = data.get("msg") or data.get("msg1") or "Unknown error"
    return {"success": False, "msg": str(msg)}


def _submit_registration(session, token: str, code: str, name: str,
                          mobile: str, otp: str, city: str) -> dict:
    payload = {
        "campaigncode": code,
        "name": name,
        "mobile": mobile,
        "mobile_otp": otp,
        "state": city,
        "question": "Japanese",
        "lda": "yes",
        "terms": "yes",
        "token": token,
        "g-recaptcha-response": "",
    }
    r = session.post(
        f"{BASE_URL}/home/register",
        data=payload,
        headers=HEADERS,
        timeout=30,
        allow_redirects=True,
    )
    text_lower = r.text.lower()
    if any(k in text_lower for k in [
        "thank you", "successfully", "registered", "congratulation",
        "success", "shukriya", "dhanyavaad",
    ]):
        return {"success": True, "msg": "Registration successful!"}

    soup = BeautifulSoup(r.text, "html.parser")
    for el in soup.find_all(class_=["text-danger", "alert", "error", "alert-danger"]):
        msg_text = el.get_text(strip=True)
        if msg_text:
            return {"success": False, "msg": msg_text[:200]}

    return {"success": False, "msg": "Unexpected response — manually verify."}


# ── Async wrappers ────────────────────────────────────────────────────────────

async def _run(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fn, *args)


# ── Core: process one code fully automatically ───────────────────────────────

async def process_code_auto(code: str, name: str, city: str,
                             update: Update, context: ContextTypes.DEFAULT_TYPE,
                             city_idx: int) -> dict:
    """
    Full automatic flow for one code:
    1. Get virtual number from OTP Doctor
    2. Request OTP on GrainOtch website
    3. Wait for SMS → extract OTP
    4. Submit registration
    5. Wait for 2nd SMS → extract voucher
    Returns result dict.
    """
    api = get_otp_doctor()
    loop = asyncio.get_event_loop()
    activation_id = None

    # Step 1: Get virtual number
    await update.message.reply_text(
        f"📱 *Code {esc(code)}* — OTP Doctor se number le raha hoon...",
        parse_mode="Markdown",
    )
    try:
        service_id = await get_grainotch_service_id()
        if not service_id:
            return {"success": False, "msg": "Grainotch service OTP Doctor mein nahi mila. /services se check karo."}

        def _buy_number():
            return api.get_number(service_id, country="in")

        number_info = await loop.run_in_executor(None, _buy_number)
        activation_id = number_info["id"]
        phone         = number_info["phone"]
        # Remove country prefix if present (e.g., 917XXXXXXXXXX → 7XXXXXXXXXX)
        if phone.startswith("91") and len(phone) == 12:
            phone_clean = phone[2:]
        else:
            phone_clean = phone.lstrip("+91")

        logger.info("Got number: %s (activation: %s)", phone, activation_id)
    except Exception as e:
        logger.error("Number buy failed: %s", e)
        return {"success": False, "msg": f"Number nahi mila: {str(e)[:100]}"}

    await update.message.reply_text(
        f"✅ Number mila: `{esc(phone_clean)}`\n"
        f"⏳ GrainOtch pe OTP request bhej raha hoon...",
        parse_mode="Markdown",
    )

    # Step 2: Get register page + request OTP
    try:
        web_session, token = await _run(_get_register_page)
        otp_result = await _run(_request_otp, web_session, token, code, phone_clean)
    except Exception as e:
        logger.error("OTP request failed: %s", e)
        if activation_id:
            await loop.run_in_executor(None, api.cancel, activation_id)
        return {"success": False, "msg": f"Website OTP error: {str(e)[:100]}"}

    if not otp_result["success"]:
        if activation_id:
            await loop.run_in_executor(None, api.cancel, activation_id)
        return {"success": False, "msg": f"OTP send failed: {otp_result['msg']}"}

    await update.message.reply_text(
        f"📨 OTP request gaya! SMS ka wait kar raha hoon (max 2 min)...",
    )

    # Step 3: Wait for OTP SMS
    def _wait_sms():
        return api.wait_for_sms(activation_id, max_wait=120, poll_interval=5)

    sms1_text = await loop.run_in_executor(None, _wait_sms)

    if not sms1_text:
        await loop.run_in_executor(None, api.cancel, activation_id)
        return {"success": False, "msg": "OTP SMS nahi aaya (timeout 2 min)"}

    logger.info("SMS 1 received: %s", sms1_text)

    otp = extract_otp(sms1_text)
    if not otp:
        await loop.run_in_executor(None, api.cancel, activation_id)
        return {"success": False, "msg": f"OTP extract nahi hua. SMS: {sms1_text[:80]}"}

    await update.message.reply_text(
        f"🔑 OTP mila: `{esc(otp)}`\n"
        f"⏳ Registration submit kar raha hoon...\n"
        f"👤 Naam: *{esc(name)}* | 🏙️ City: *{esc(city)}*",
        parse_mode="Markdown",
    )

    # Step 4: Submit registration
    try:
        reg_result = await _run(_submit_registration, web_session, token,
                                code, name, phone_clean, otp, city)
    except Exception as e:
        logger.error("Registration submit failed: %s", e)
        reg_result = {"success": False, "msg": f"Network error: {str(e)[:80]}"}

    if not reg_result["success"]:
        await loop.run_in_executor(None, api.cancel, activation_id)
        return {
            "success": False,
            "msg": reg_result["msg"],
            "phone": phone_clean,
            "otp": otp,
        }

    await update.message.reply_text(
        f"✅ *Registration ho gayi!*\n\n"
        f"⏳ Amazon Voucher wala 2nd SMS dhundh raha hoon...\n"
        f"_(Max 5 min wait karunga — har 30s pe re-check karunga)_ 🎁",
        parse_mode="Markdown",
    )

    # Step 5: Wait for 2nd SMS (voucher) — 5 min window, re-sends status=8 every 30s
    def _wait_sms2():
        return api.wait_for_second_sms(activation_id, max_wait=300, poll_interval=6)

    sms2_text = await loop.run_in_executor(None, _wait_sms2)

    voucher = None
    if sms2_text:
        logger.info("SMS 2 received: %s", sms2_text)
        voucher = extract_voucher(sms2_text)
        # Finish the activation
        await loop.run_in_executor(None, api.finish, activation_id)
    else:
        logger.info("No 2nd SMS received (timeout)")
        await loop.run_in_executor(None, api.finish, activation_id)

    return {
        "success": True,
        "msg": "Registration + Voucher complete!",
        "phone": phone_clean,
        "otp": otp,
        "sms1": sms1_text,
        "sms2": sms2_text,
        "voucher": voucher,
        "name": name,
        "city": city,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def esc(text: str) -> str:
    return str(text).replace("_", r"\_").replace("*", r"\*") \
                    .replace("`", r"\`").replace("[", r"\[")


# ── /mevo — full report ───────────────────────────────────────────────────────

async def mevo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    if not db:
        await update.message.reply_text(
            "📭 Abhi koi code database mein nahi hai.\n/start karke codes bhejo."
        )
        return

    used    = [r for r in db.values() if r.get("status") == "success"]
    failed  = [r for r in db.values() if r.get("status") == "failed"]
    pending = [r for r in db.values() if r.get("status") == "pending"]

    lines = [f"📋 *FULL CODE REPORT* ({len(db)} total)\n"]

    lines.append(f"✅ *Registered ({len(used)}):*")
    for r in used:
        voucher = r.get("voucher", "")
        voucher_str = f" 🎁`{esc(voucher)}`" if voucher else " _(no voucher)_"
        lines.append(f"  `{esc(r['code'])}` — {esc(r.get('name','-'))}, {esc(r.get('city','-'))}{voucher_str}")

    lines.append("")
    lines.append(f"❌ *Failed ({len(failed)}):*")
    for r in failed:
        lines.append(f"  `{esc(r['code'])}` — {esc(r.get('error','?')[:60])}")
    if not failed:
        lines.append("  _koi nahi_")

    lines.append("")
    lines.append(f"🕐 *Pending ({len(pending)}):*")
    for r in pending:
        lines.append(f"  `{esc(r['code'])}`")
    if not pending:
        lines.append("  _koi nahi_")

    full_msg = "\n".join(lines)
    for i in range(0, len(full_msg), 4000):
        await update.message.reply_text(full_msg[i:i+4000], parse_mode="Markdown")


# ── /balance ───────────────────────────────────────────────────────────────────

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        api = get_otp_doctor()
        loop = asyncio.get_event_loop()
        bal  = await loop.run_in_executor(None, api.get_balance)
        await update.message.reply_text(f"💰 OTP Doctor balance: *₹{bal:.2f}*", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Balance error: {e}")


# ── /services — show available service IDs ────────────────────────────────────

async def services_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 OTP Doctor services dhundh raha hoon...")
    try:
        api  = get_otp_doctor()
        loop = asyncio.get_event_loop()
        services = await loop.run_in_executor(None, api.get_services)
        if not services:
            await update.message.reply_text("❌ Services nahi mili.")
            return

        # Search grainotch
        grain_entries = [(k, v) for k, v in services.items()
                         if "grain" in v.get("service_name", "").lower()]

        if grain_entries:
            lines = ["🌾 *Grainotch Services:*"]
            for sid, info in grain_entries:
                lines.append(
                    f"  ID `{sid}`: {esc(info['service_name'])} — "
                    f"₹{info['service_price']} — {esc(info['server_name'])}"
                )
        else:
            lines = ["⚠️ Grainotch nahi mila. Available servers (first 20):"]
            for sid, info in list(services.items())[:20]:
                lines.append(f"  `{sid}`: {esc(info['service_name'])} ({esc(info['server_name'])})")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


# ── /setservice — manually set service ID ────────────────────────────────────

async def setservice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _grainotch_service_id
    args = context.args
    if not args:
        sid = _grainotch_service_id or "not set"
        await update.message.reply_text(
            f"Current service ID: `{esc(sid)}`\n\n"
            f"Change karne ke liye: `/setservice <ID>`\n"
            f"Example: `/setservice 9622`",
            parse_mode="Markdown",
        )
        return
    _grainotch_service_id = args[0].strip()
    await update.message.reply_text(
        f"✅ Service ID set: `{esc(_grainotch_service_id)}`", parse_mode="Markdown"
    )


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    db = load_db()
    pending = [r["code"] for r in db.values() if r.get("status") == "pending"]

    if pending:
        context.user_data["codes"]    = pending
        context.user_data["city_idx"] = 0
        await update.message.reply_text(
            f"🔄 *{len(pending)} pending code(s) mile!* Auto-process shuru hoga.\n\n"
            f"📋 Codes: `{esc(', '.join(pending))}`\n\n"
            f"▶️ `/run` bhejo process shuru karne ke liye\n"
            f"🆕 `/newcodes` bhejo naye codes dene ke liye",
            parse_mode="Markdown",
        )
        return CODES

    await update.message.reply_text(
        "🥃 *GrainOtch Auto-Registration Bot*\n\n"
        "📋 *Codes bhejo* — comma ya newline se alag karo:\n\n"
        "`ABC123DEF4, XYZ987WQR1`\n\n"
        "⚠️ 10-character codes (A-Z, 0-9)\n\n"
        "📊 /mevo — codes ka report\n"
        "💰 /balance — OTP Doctor balance\n"
        "🔍 /services — Grainotch service check\n"
        "🚫 /cancel — band karo",
        parse_mode="Markdown",
    )
    return CODES


# ── /newcodes ─────────────────────────────────────────────────────────────────

async def newcodes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "📋 *Naye codes bhejo* — comma ya newline se:\n`ABC123DEF4, XYZ987WQR1`",
        parse_mode="Markdown",
    )
    return CODES


# ── Receive codes + auto-process ──────────────────────────────────────────────

async def receive_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    # /run command during CODES state
    if text.lower() in ["/run", "run"]:
        codes = context.user_data.get("codes", [])
        if not codes:
            await update.message.reply_text("Koi codes nahi hain. Pehle codes bhejo.")
            return CODES
        await start_auto_processing(update, context, codes)
        return ConversationHandler.END

    raw        = re.split(r"[,\n\r\t]+", text)
    codes      = [c.strip().upper() for c in raw if c.strip()]
    valid      = [c for c in codes if len(c) == 10 and c.isalnum()]
    invalid    = [c for c in codes if c not in valid]

    if not valid:
        await update.message.reply_text(
            "❌ Koi valid code nahi mila.\n10-character codes bhejo (A-Z, 0-9)."
        )
        return CODES

    db = load_db()
    for c in valid:
        if c not in db:
            await upsert_code(c, "pending")

    db          = load_db()
    already_done = [c for c in valid if db.get(c, {}).get("status") == "success"]
    to_process  = [c for c in valid if db.get(c, {}).get("status") != "success"]

    msg = f"✅ *{len(to_process)} code(s) process honge!*\n"
    if already_done:
        msg += f"⏭️ Already registered skip: `{esc(', '.join(already_done))}`\n"
    if invalid:
        msg += f"⚠️ Invalid skip: `{esc(', '.join(invalid))}`\n"

    if not to_process:
        await update.message.reply_text(msg + "\nSaare codes pehle se done hain!")
        return CODES

    await update.message.reply_text(
        msg + f"\n🤖 *Fully automatic mode!*\n"
        f"OTP Doctor se numbers lega → OTP auto-submit → Voucher save karega\n\n"
        f"▶️ *Processing shuru hoti hai...*",
        parse_mode="Markdown",
    )

    await start_auto_processing(update, context, to_process)
    return ConversationHandler.END


async def start_auto_processing(update: Update, context: ContextTypes.DEFAULT_TYPE, codes: list) -> None:
    """Process all codes automatically one by one."""
    total        = len(codes)
    success_list = []
    fail_list    = []
    voucher_list = []
    city_idx     = 0

    for i, code in enumerate(codes, 1):
        name = random.choice(INDIAN_MALE_NAMES)
        city = CITIES[city_idx % len(CITIES)]
        city_idx += 1

        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔄 *Code {i}/{total}:* `{esc(code)}`\n"
            f"👤 Naam: *{esc(name)}* | 🏙️ City: *{esc(city)}*",
            parse_mode="Markdown",
        )

        try:
            result = await process_code_auto(code, name, city, update, context, city_idx)
        except Exception as e:
            logger.error("Unexpected error for code %s: %s", code, e)
            result = {"success": False, "msg": f"Unexpected error: {str(e)[:100]}"}

        if result["success"]:
            voucher = result.get("voucher")
            await upsert_code(
                code, "success",
                name=name, city=city,
                mobile=result.get("phone", ""),
                otp=result.get("otp", ""),
                sms1=result.get("sms1", ""),
                sms2=result.get("sms2", ""),
                voucher=voucher or "",
            )
            success_list.append(code)

            sms2_raw = result.get("sms2", "")
            if voucher:
                voucher_list.append((code, voucher))
                voucher_msg = (
                    f"\n\n🎁 *Amazon Voucher Mila!*\n"
                    f"┌─────────────────────\n"
                    f"│ `{esc(voucher)}`\n"
                    f"└─────────────────────\n"
                    f"📩 Full SMS: _{esc(sms2_raw)}_"
                )
            elif sms2_raw:
                voucher_msg = (
                    f"\n\n⚠️ *2nd SMS aaya par code extract nahi hua*\n"
                    f"📩 Full SMS: _{esc(sms2_raw)}_"
                )
            else:
                voucher_msg = "\n\n⚠️ Amazon Voucher SMS nahi aaya (timeout)"

            await update.message.reply_text(
                f"✅ `{esc(code)}` — *Register ho gaya!*\n"
                f"📱 Number: `{esc(result.get('phone','?'))}`"
                f"{voucher_msg}",
                parse_mode="Markdown",
            )
        else:
            await upsert_code(
                code, "failed",
                error=result["msg"],
                mobile=result.get("phone", ""),
            )
            fail_list.append(code)
            await update.message.reply_text(
                f"❌ `{esc(code)}` — *Failed:* {esc(result['msg'])}",
                parse_mode="Markdown",
            )

        # Small delay between codes
        if i < total:
            await asyncio.sleep(3)

    # ── Final summary ─────────────────────────────────────────────────────────
    summary = (
        f"🎉 *Saare codes process ho gaye!*\n\n"
        f"📊 *Result: {len(success_list)}/{total} successful*\n\n"
    )

    if voucher_list:
        summary += f"🎁 *Amazon Vouchers ({len(voucher_list)}):*\n"
        for code, v in voucher_list:
            summary += f"  `{esc(code)}` → `{esc(v)}`\n"
        summary += "\n"

    if fail_list:
        summary += f"❌ *Failed codes:* `{esc(', '.join(fail_list))}`\n"

    summary += "\n📋 /mevo se poori list | /start se naye codes"
    await update.message.reply_text(summary, parse_mode="Markdown")


# ── /run command ──────────────────────────────────────────────────────────────

async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run pending codes from DB."""
    db      = load_db()
    pending = [r["code"] for r in db.values() if r.get("status") == "pending"]
    if not pending:
        await update.message.reply_text(
            "✅ Koi pending code nahi hai.\n/start se naye codes do."
        )
        return
    await update.message.reply_text(
        f"▶️ *{len(pending)} pending codes process ho rahe hain...*",
        parse_mode="Markdown",
    )
    await start_auto_processing(update, context, pending)


# ── /cancel ───────────────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Process cancel.\n/start se dobara shuru karo.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── /export ───────────────────────────────────────────────────────────────────

async def export_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    if not db:
        await update.message.reply_text("📭 Database mein koi code nahi.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Code", "Status", "Name", "City", "Mobile", "OTP", "Voucher", "SMS1", "SMS2", "Error", "Added At"])
    for r in db.values():
        writer.writerow([
            r.get("code", ""), r.get("status", ""), r.get("name", ""),
            r.get("city", ""), r.get("mobile", ""), r.get("otp", ""),
            r.get("voucher", ""), r.get("sms1", ""), r.get("sms2", ""),
            r.get("error", ""), r.get("added_at", ""),
        ])

    buf.seek(0)
    fb = io.BytesIO(buf.getvalue().encode("utf-8"))
    fb.name = "codes_report.csv"

    used    = sum(1 for r in db.values() if r.get("status") == "success")
    failed  = sum(1 for r in db.values() if r.get("status") == "failed")
    pending = sum(1 for r in db.values() if r.get("status") == "pending")
    vouchers = sum(1 for r in db.values() if r.get("voucher"))

    await update.message.reply_document(
        document=fb,
        filename="codes_report.csv",
        caption=(
            f"📊 *Codes Report*\n"
            f"Total: {len(db)} | ✅ {used} | ❌ {failed} | 🕐 {pending} | 🎟️ {vouchers} vouchers"
        ),
        parse_mode="Markdown",
    )


# ── /vouchers — show all saved vouchers ──────────────────────────────────────

async def vouchers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    voucher_records = [(r["code"], r["voucher"]) for r in db.values()
                       if r.get("voucher") and r.get("status") == "success"]

    if not voucher_records:
        await update.message.reply_text("🎁 Abhi koi Amazon Voucher save nahi hua.")
        return

    lines = [f"🎁 *Amazon Vouchers ({len(voucher_records)}):*\n"]
    for code, v in voucher_records:
        lines.append(f"  `{esc(code)}` → `{esc(v)}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /redeem — clean copy-paste Amazon voucher list ───────────────────────────

async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all Amazon voucher codes as a clean copy-paste list, sorted by date."""
    db = load_db()

    records = [
        r for r in db.values()
        if r.get("voucher") and r.get("status") == "success"
    ]

    if not records:
        await update.message.reply_text(
            "🎁 Abhi koi Amazon Voucher save nahi hua.\n"
            "Codes process karne ke baad yahan aayenge."
        )
        return

    # Sort by updated_at date (newest first)
    records.sort(key=lambda r: r.get("updated_at", ""), reverse=True)

    total = len(records)

    # ── Header ──
    header = (
        f"🎁 *Amazon Vouchers — {total} code(s)*\n"
        f"_(Newest first | Copy karke amazon.in pe redeem karo)_\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    # ── Voucher lines — each on its own for easy copying ──
    voucher_lines = []
    for i, r in enumerate(records, 1):
        voucher   = r["voucher"]
        code      = r.get("code", "?")
        timestamp = r.get("updated_at", "")[:10]  # just the date
        voucher_lines.append(
            f"*{i}.* `{esc(voucher)}`\n"
            f"     📋 Code: `{esc(code)}` | 📅 {timestamp}"
        )

    # ── Plain text block for bulk copy ──
    plain_codes = "\n".join(r["voucher"] for r in records)
    plain_block = (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 *Sirf Codes (bulk copy):*\n"
        f"```\n{plain_codes}\n```"
    )

    full_msg = header + "\n\n".join(voucher_lines) + plain_block

    # Telegram 4096 char limit — split if needed
    if len(full_msg) <= 4096:
        await update.message.reply_text(full_msg, parse_mode="Markdown")
    else:
        # Send header + individual vouchers
        await update.message.reply_text(header + "\n\n".join(voucher_lines), parse_mode="Markdown")
        # Send plain bulk copy block separately
        await update.message.reply_text(
            f"📋 *Bulk Copy ({total} codes):*\n```\n{plain_codes}\n```",
            parse_mode="Markdown",
        )


# ── Error handler ─────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception:", exc_info=context.error)
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(
            "⚠️ Kuch error aa gaya. /cancel karke /start se dobara try karo."
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN nahi mila!")

    app = Application.builder().token(bot_token).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("newcodes", newcodes),
        ],
        states={
            CODES: [
                CommandHandler("run", receive_codes),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_codes),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("run",        run_cmd))
    app.add_handler(CommandHandler("cancel",     cancel))
    app.add_handler(CommandHandler("mevo",       mevo))
    app.add_handler(CommandHandler("export",     export_codes))
    app.add_handler(CommandHandler("balance",    balance_cmd))
    app.add_handler(CommandHandler("services",   services_cmd))
    app.add_handler(CommandHandler("setservice", setservice_cmd))
    app.add_handler(CommandHandler("vouchers",   vouchers_cmd))
    app.add_handler(CommandHandler("redeem",     redeem_cmd))
    app.add_error_handler(error_handler)

    # Pre-fetch Grainotch service ID at startup
    app.post_init = _prefetch_service_id

    logger.info("Bot starting (fully automatic mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
