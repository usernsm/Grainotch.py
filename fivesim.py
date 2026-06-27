"""
5sim.net API wrapper
Docs: https://5sim.net/docs
"""

import time
import requests

BASE_URL = "https://5sim.net/v1"


class FiveSimAPI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })

    def get_balance(self) -> dict:
        """Apna balance dekho."""
        r = self.session.get(f"{BASE_URL}/user/profile", timeout=15)
        r.raise_for_status()
        data = r.json()
        return {"balance": data.get("balance", 0), "raw": data}

    def get_number(self, country: str = "india", operator: str = "any",
                   product: str = "other") -> dict:
        """
        Virtual number kharido.
        country: 'india', 'russia', etc.
        operator: 'any', 'airtel', 'jio', etc.
        product: 'other', 'telegram', etc.
        Returns: {id, phone, status}
        """
        url = f"{BASE_URL}/user/buy/activation/{country}/{operator}/{product}"
        r = self.session.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        return {
            "id": data.get("id"),
            "phone": data.get("phone"),
            "status": data.get("status"),
            "raw": data,
        }

    def get_sms(self, order_id: int) -> list:
        """
        Order ke liye SMS check karo.
        Returns list of received SMS.
        """
        r = self.session.get(f"{BASE_URL}/user/check/{order_id}", timeout=15)
        r.raise_for_status()
        data = r.json()
        sms_list = data.get("sms", [])
        return sms_list

    def finish_order(self, order_id: int) -> dict:
        """Order complete karo (number release)."""
        r = self.session.get(f"{BASE_URL}/user/finish/{order_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, order_id: int) -> dict:
        """Order cancel karo."""
        r = self.session.get(f"{BASE_URL}/user/cancel/{order_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    def ban_order(self, order_id: int) -> dict:
        """Order ban karo (galat number mila)."""
        r = self.session.get(f"{BASE_URL}/user/ban/{order_id}", timeout=15)
        r.raise_for_status()
        return r.json()

    def wait_for_sms(self, order_id: int, max_wait: int = 120,
                     poll_interval: int = 5) -> list:
        """
        SMS aane tak wait karo.
        max_wait: max seconds (default 120)
        poll_interval: check every N seconds (default 5)
        Returns list of SMS dicts, empty list if timeout.
        """
        waited = 0
        while waited < max_wait:
            sms_list = self.get_sms(order_id)
            if sms_list:
                return sms_list
            time.sleep(poll_interval)
            waited += poll_interval
        return []

    def wait_for_all_sms(self, order_id: int, expected_count: int = 2,
                         max_wait: int = 180, poll_interval: int = 5) -> list:
        """
        Multiple SMS aane tak wait karo (OTP + voucher).
        expected_count: kitne SMS chahiye (default 2)
        Returns list of all received SMS.
        """
        waited = 0
        while waited < max_wait:
            sms_list = self.get_sms(order_id)
            if len(sms_list) >= expected_count:
                return sms_list
            time.sleep(poll_interval)
            waited += poll_interval
        return self.get_sms(order_id)


if __name__ == "__main__":
    import os
    api_key = os.environ.get("FIVESIM_API_KEY", "YOUR_API_KEY_HERE")
    api = FiveSimAPI(api_key)

    print("=== 5sim.net API Test ===")

    bal = api.get_balance()
    print(f"Balance: {bal['balance']}")

    print("\nNumber le raha hoon (india, any, other)...")
    order = api.get_number(country="india", operator="any", product="other")
    print(f"Order ID: {order['id']}")
    print(f"Phone: {order['phone']}")
    print(f"Status: {order['status']}")

    if order["id"]:
        print(f"\nSMS wait kar raha hoon (max 2 min)...")
        sms_list = api.wait_for_all_sms(order["id"], expected_count=2)
        if sms_list:
            for i, sms in enumerate(sms_list, 1):
                print(f"\nSMS #{i}:")
                print(f"  Text: {sms.get('text', '')}")
                print(f"  Time: {sms.get('created_at', '')}")
        else:
            print("Timeout — koi SMS nahi aaya.")
        api.finish_order(order["id"])
        print("\nOrder complete.")
