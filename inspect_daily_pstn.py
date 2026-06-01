import json
import os

import dotenv
import requests

dotenv.load_dotenv()
api_key = os.getenv("DAILY_API_KEY")
headers = {"Authorization": f"Bearer {api_key}"}
base_url = "https://api.daily.co/v1"


def get_and_print(path):
    print(f"\n--- {path} ---")
    r = requests.get(f"{base_url}{path}", headers=headers)
    print(json.dumps(r.json(), indent=2))
    return r.json()


data = get_and_print("/purchased-phone-numbers")
get_and_print("/")
if data.get("data"):
    for item in data["data"]:
        get_and_print(f"/purchased-phone-numbers/{item['id']}")
