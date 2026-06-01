import os
from pathlib import Path

import requests


def load_dotenv():
    env_file = Path(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if not os.environ.get(key.strip()):
            os.environ[key.strip()] = value.strip()


load_dotenv()
api_key = os.environ.get("DAILY_API_KEY")
resp = requests.get("https://api.daily.co/v1/", headers={"Authorization": f"Bearer {api_key}"})
print(resp.text)
