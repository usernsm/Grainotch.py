"""
sms-activate.org API wrapper
Docs: https://sms-activate.org/en/api2
"""

import re
import time
import requests

BASE_URL = "https://api.sms-activate.org/stubs/handler_api.php"


class SmsActivateAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def _get(self, params: dict) -> str:
        params["api_key"] = self.api_key
        r = self.session.get(BASE_URL, params=params, timeout=15)
        r.raise_for_status()
        return r.text.strip()

    def get_balance(self) -> float:
        """Apna balance dekho."""
        resp = self._get({"action": "getBalance"})
        if resp.startswith("ACCESS_BALANCE:"):
            return float(resp.split(":")[1])
        raise Exception(f"Balance error: {resp}")

    def get_number(self, service: str = "ot", country: str = "22") -> dict:
        """
        Virtual number kharido.
        service: 'ot' = other, 'tg' = telegram, 'wb' = whatsapp
        country: '22' = India, '0' = Russia, '6' = Indonesia
        Returns: {id, phone}
        """
        resp = self._get({
            "action": "getNumber",
            "service": service,
            "country": country,
        })
        if resp.startswith("ACCESS_NUMBER:"):
            parts = resp.split(":")
            return {"id": int(parts[1]), "phone": parts[2]}
        raise Exception(f"Get number error: {resp}")

    def get_status(self, activation_id: int) -> dict:
        """
        SMS status check karo.
        Returns: {status, code/text}
        Possible statuses:
          STATUS_WAIT_CODE  — waiting
          STATUS_CANCEL     — cancelled
          STATUS_OK:<code>  — SMS received
        """
        resp = self._get({
            "action": "getStatus",
            "id": activation_id,
        })
        if resp == "STATUS_WAIT_CODE":
            return {"status": "waiting", "text": None}
        elif resp == "STATUS_CANCEL":
            return {"status": "cancelled", "text": None}
        elif resp.startswith("STATUS_OK:"):
            return {"status": "ok", "text": resp.split(":", 1)[1]}
        elif resp == "STATUS_WAIT_RESEND":
            return {"status": "waiting_resend", "text": None}
        return {"status": "unknown", "raw": resp, "text": None}

    def set_status(self, activation_id: int, status: int) -> str:
        """
        Status set karo.
        status codes:
          1  = ready to receive SMS (request resend)
          3  = activation complete
          6  = cancel activation
          8  = SMS received, request another
        """
        resp = self._get({
            "action": "setStatus",
            "id": activation_id,
            "status": status,
        })
        return resp

    def cancel(self, activation_id: int) -> str:
        """Order cancel karo."""
        return self.set_status(activation_id, 6)

    def finish(self, activation_id: int) -> str:
        """Order complete karo."""
        return self.set_status(activation_id, 3)

    def wait_for_sms(self, activation_id: int, max_wait: int = 120,
                     poll_interval: int = 5) -> str | None:
        """
        Pehla SMS aane tak wait karo.
        Returns SMS text ya None if timeout.
        """
        waited = 0
        while waited < max_wait:
            result = self.get_status(activation_id)
            if result["status"] == "ok":
                return result["text"]
            elif result["status"] == "cancelled":
                return None
            time.sleep(poll_interval)
            waited += poll_interval
        return None

    def wait_for_second_sms(self, activation_id: int, max_wait: int = 120,
                             poll_interval: int = 5) -> str | None:
        """
        Pehla SMS aane ke baad, dusra SMS bhi wait karo.
        Pehle set_status(8) karo — "SMS mila, aur chahiye".
        """
        self.set_status(activation_id, 8)
        return self.wait_for_sms(activation_id, max_wait, poll_interval)


if __name__ == "__main__":
    import os
    api_key = os.environ.get("SMSACTIVATE_API_KEY", "YOUR_API_KEY_HERE")
    api = SmsActivateAPI(api_key)

    print("=== sms-activate.org API Test ===")

    bal = api.get_balance()
    print(f"Balance: {bal}")

    print("\nNumber le raha hoon (India, other service)...")
    order = api.get_number(service="ot", country="22")
    print(f"Activation ID: {order['id']}")
    print(f"Phone: {order['phone']}")

    print(f"\nPehla SMS wait kar raha hoon (max 2 min)...")
    sms1 = api.wait_for_sms(order["id"])
    if sms1:
        print(f"SMS 1: {sms1}")

        print("\nDusra SMS wait kar raha hoon (voucher)...")
        sms2 = api.wait_for_second_sms(order["id"])
        if sms2:
            print(f"SMS 2 (Voucher): {sms2}")
        else:
            print("Dusra SMS nahi aaya.")

        api.finish(order["id"])
        print("Order complete.")
    else:
        print("Timeout — koi SMS nahi aaya.")
        api.cancel(order["id"])
