import os
import requests

SUPABASE_URL = "https://hkcbnibbguzbgqucnkzm.supabase.co"
SUPABASE_KEY = "sb_publishable_Kx0gAQwUHagHrNyFZo7xjg_7Y72LCFn"

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

# Check if table exists
r = requests.get(f"{SUPABASE_URL}/rest/v1/records?limit=1", headers=headers)
print("GET /records:", r.status_code, r.text)
