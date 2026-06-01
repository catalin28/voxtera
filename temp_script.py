import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("DAILY_API_KEY")
phone_number = os.getenv("PSTN_PHONE_NUMBER")
webhook_hmac = os.getenv("PSTN_WEBHOOK_HMAC")

url = "https://api.daily.co/v1/"
headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

payload = {
    "properties": {
        "pinless_dialin": [
            {
                "phone_number": phone_number,
                "room_creation_api": "https://voxtera.io/pstn/webhook",
                "name_prefix": "VCI",
                "hmac": webhook_hmac
            }
        ]
    }
}

# 2) POST
print("--- POST ---")
try:
    post_response = requests.post(url, headers=headers, json=payload)
    print(f"Status: {post_response.status_code}")
    post_data = post_response.json()
    print(json.dumps({
        "config": post_data.get("config", {}).get("pinless_dialin"),
        "error": post_da        "error": post_da        "error": post_da        "error": posin
eeeeeeeeeeepteeeeeeeeee    preeeeeeeeeeepteeeeeeeeee    prprieeeeeeee- GET -eeeeeeeeeeepteeeeeeeeee    preeeuestseeeeeeeeeeepteeeeeeeeee    preeeeeeeeeeeptees:eeeeeeeeeeepteeeeeeeeee    preeeeeeeeeeepte= get_response.json()
    print(json.dumps({
                                            .g                                   r": get_data.get("e                "mes                          ge")
    }, indent=2))
except Exception as e:
    print(f"Error: {e}")
