"""
OTP Doctor API wrapper (sms-activate compatible)
Base: http://otpdoctor.in/stubs/handler_api.php
"""

import re
import time
import logging
import requests

logger = logging.getLogger(__name__)

BASE_URL_HTTP  = "http://otpdoctor.in/stubs/handler_api.php"
BASE_URL_HTTPS = "https://otpdoctor.in/stubs/handler_api.php"


class OTPDoctorAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self._service_cache: dict = {}

    def _get(self, params: dict, timeout: int = 15) -> str:
        """
        Try HTTP first, then HTTPS on failure — OTP Doctor is intermittent.
        Each URL gets 2 attempts before switching.
        """
        params_full = {"api_key": self.api_key, **params}
        urls = [BASE_URL_HTTP, BASE_URL_HTTPS, BASE_URL_HTTP]
        last_err = None
        for i, url in enumerate(urls):
            try:
                r = self.session.get(url, params=params_full, timeout=timeout)
                r.raise_for_status()
                return r.text.strip()
            except Exception as e:
                last_err = e
                logger.debug("_get attempt %d failed (%s): %s", i + 1, url[:30], str(e)[:80])
                time.sleep(1)
        raise last_err

    # ── Account ──────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        resp = self._get({"action": "getBalance"})
        if resp.startswith("ACCESS_BALANCE:"):
            return float(resp.split(":")[1])
        raise Exception(f"Balance error: {resp}")

    # ── Services ─────────────────────────────────────────────────────────────

    def get_services(self) -> dict:
        """
        Returns dict: {service_id: {service_name, service_price, server_name}}
        MUST pass country='in' — without it API returns BAD_COUNTRY.
        """
        import json as _json

        for attempt in range(3):
            try:
                resp = self._get({"action": "getServices", "country": "in"}, timeout=20)
                if resp.startswith("{"):
                    data = _json.loads(resp)
                    if data:
                        self._service_cache = data
                        logger.info("getServices OK: %d services", len(data))
                        return data
                logger.warning("getServices bad response (attempt %d): %s", attempt + 1, resp[:60])
            except Exception as e:
                logger.warning("getServices error (attempt %d): %s", attempt + 1, e)
            time.sleep(3)

        logger.error("getServices failed — returning cached (%d items)", len(self._service_cache))
        return self._service_cache

    def find_service_id(self, service_name_keyword: str, server_name_keyword: str = "") -> str | None:
        """
        Search service list for matching service.
        Returns service_id (str) or None.
        """
        services = self.get_services()
        if not services:
            return None

        keyword_lower = service_name_keyword.lower()
        server_lower  = server_name_keyword.lower()

        # Exact match first
        for sid, info in services.items():
            sname = info.get("service_name", "").lower()
            svname = info.get("server_name", "").lower()
            if keyword_lower in sname and (not server_lower or server_lower in svname):
                return sid

        return None

    # ── Numbers ──────────────────────────────────────────────────────────────

    def get_number(self, service_id: str, country: str = "in") -> dict:
        """
        Buy a virtual number.
        service_id: numeric ID from get_services()
        country: 'in' for India
        Returns: {id, phone}
        """
        resp = self._get({
            "action": "getNumber",
            "service": service_id,
            "country": country,
        })
        if resp.startswith("ACCESS_NUMBER:"):
            parts = resp.split(":")
            return {"id": parts[1], "phone": parts[2]}
        raise Exception(f"getNumber error: {resp}")

    def get_status(self, activation_id: str) -> dict:
        """
        Check SMS status for an activation.
        Returns: {status, text}
          status: 'waiting' | 'ok' | 'cancelled' | 'waiting_resend' | 'unknown'
        """
        resp = self._get({"action": "getStatus", "id": activation_id})
        if resp == "STATUS_WAIT_CODE":
            return {"status": "waiting", "text": None}
        elif resp == "STATUS_CANCEL":
            return {"status": "cancelled", "text": None}
        elif resp.startswith("STATUS_OK:"):
            return {"status": "ok", "text": resp.split(":", 1)[1]}
        elif resp == "STATUS_WAIT_RESEND":
            return {"status": "waiting_resend", "text": None}
        return {"status": "unknown", "raw": resp, "text": None}

    def set_status(self, activation_id: str, status: int) -> str:
        """
        Set activation status:
          1 = ready / resend SMS
          3 = activation complete (finish)
          6 = cancel
          8 = SMS received, need another (for 2nd SMS / voucher)
        """
        return self._get({"action": "setStatus", "id": activation_id, "status": status})

    def cancel(self, activation_id: str) -> str:
        return self.set_status(activation_id, 6)

    def finish(self, activation_id: str) -> str:
        return self.set_status(activation_id, 3)

    # ── Wait helpers ─────────────────────────────────────────────────────────

    def wait_for_sms(self, activation_id: str, max_wait: int = 120,
                     poll_interval: int = 5) -> str | None:
        """
        Wait for first SMS. Returns SMS text or None on timeout/cancel.
        """
        waited = 0
        while waited < max_wait:
            try:
                result = self.get_status(activation_id)
                if result["status"] == "ok":
                    return result["text"]
                elif result["status"] == "cancelled":
                    logger.warning("Activation %s cancelled", activation_id)
                    return None
            except Exception as e:
                logger.warning("get_status error: %s", e)
            time.sleep(poll_interval)
            waited += poll_interval
        return None

    def wait_for_second_sms(self, activation_id: str, max_wait: int = 300,
                             poll_interval: int = 6) -> str | None:
        """
        After first SMS, request second SMS (voucher/reward).
        Retries set_status(8) every 30s in case the first call timed out.
        max_wait default = 300s (5 min) — voucher SMS can be slow.
        """
        # First signal: tell OTP Doctor we want the 2nd SMS
        for attempt in range(4):
            try:
                resp = self.set_status(activation_id, 8)
                logger.info("set_status(8) attempt %d: %s", attempt + 1, resp)
                break
            except Exception as e:
                logger.warning("set_status(8) attempt %d failed: %s", attempt + 1, e)
                time.sleep(3)

        # Now poll for 2nd SMS, re-sending status=8 every 30s in case it was missed
        waited = 0
        next_resend = 30  # resend set_status(8) every 30 seconds
        while waited < max_wait:
            try:
                result = self.get_status(activation_id)
                status = result["status"]
                if status == "ok":
                    logger.info("2nd SMS received after %ds", waited)
                    return result["text"]
                elif status == "cancelled":
                    logger.warning("Activation %s cancelled while waiting for 2nd SMS", activation_id)
                    return None
                elif status in ("waiting", "waiting_resend", "unknown"):
                    # Re-send the "give me 2nd SMS" signal periodically
                    if waited >= next_resend:
                        try:
                            self.set_status(activation_id, 8)
                            logger.debug("Re-sent set_status(8) at %ds", waited)
                        except Exception as e:
                            logger.debug("Re-send set_status(8) error: %s", e)
                        next_resend += 30
            except Exception as e:
                logger.warning("get_status error (waited %ds): %s", waited, e)

            time.sleep(poll_interval)
            waited += poll_interval

        logger.warning("2nd SMS timeout after %ds for activation %s", max_wait, activation_id)
        return None


def extract_otp(sms_text: str) -> str | None:
    """Extract 4-8 digit OTP from SMS text."""
    # Try 6-digit first (most common)
    m = re.search(r'\b(\d{6})\b', sms_text)
    if m:
        return m.group(1)
    # Try 4-digit
    m = re.search(r'\b(\d{4})\b', sms_text)
    if m:
        return m.group(1)
    # Try 8-digit
    m = re.search(r'\b(\d{8})\b', sms_text)
    if m:
        return m.group(1)
    return None


def extract_voucher(sms_text: str) -> str | None:
    """
    Extract Amazon voucher / gift card code from SMS text.
    Amazon gift card formats:
      - XXXX-XXXXXX-XXXX  (e.g. AB12-CD3456-EF78)
      - XXXX-XXXX-XXXX-XXXX
      - Plain alphanumeric 14-16 chars
    """
    # 1. Amazon standard gift card: groups separated by hyphens
    m = re.search(r'\b([A-Z0-9]{4}-[A-Z0-9]{4,6}-[A-Z0-9]{4}(?:-[A-Z0-9]{4})?)\b', sms_text)
    if m:
        return m.group(1)

    # 2. Keyword-prefixed codes (case-insensitive)
    keyword_patterns = [
        r'(?i)amazon\s*(?:gift\s*card|voucher|code|gc)[:\s#]*([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})*)',
        r'(?i)(?:voucher|gift\s*card|gift\s*code|claim\s*code|redeem)[:\s#]+([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})*)',
        r'(?i)code\s*[:\s]+([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})*)',
    ]
    for pattern in keyword_patterns:
        m = re.search(pattern, sms_text)
        if m:
            return m.group(1).upper()

    # 3. Long alphanumeric block (14-16 chars, likely a code)
    m = re.search(r'\b([A-Z0-9]{14,16})\b', sms_text)
    if m:
        return m.group(1)

    # 4. Fallback: any 10-13 char uppercase alphanumeric
    m = re.search(r'\b([A-Z0-9]{10,13})\b', sms_text)
    if m:
        return m.group(1)

    return None


if __name__ == "__main__":
    import os
    api_key = os.environ.get("OTPDOCTOR_API_KEY", "")
    api = OTPDoctorAPI(api_key)

    print("=== OTP Doctor API Test ===")
    try:
        bal = api.get_balance()
        print(f"Balance: ₹{bal}")
    except Exception as e:
        print(f"Balance error: {e}")

    print("\nSearching for Grainotch service...")
    sid = api.find_service_id("grainotch", "multisms")
    if sid:
        services = api.get_services()
        info = services.get(sid, {})
        print(f"Found! ID={sid}: {info}")
    else:
        print("Grainotch not found. Showing all services with 'MultiSms':")
        services = api.get_services()
        for k, v in list(services.items())[:30]:
            print(f"  {k}: {v}")
