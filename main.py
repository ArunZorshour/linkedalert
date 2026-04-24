from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import requests
import time
import threading
import hashlib
import os
import json
from datetime import datetime
from supabase import create_client, Client

app = FastAPI(title="LinkedAlert API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

active_monitors = {}

class MonitorCreate(BaseModel):
    name: str
    keywords: List[str]
    location: str
    interval_minutes: int = 15
    telegram_token: str
    telegram_chat_id: str
    linkedin_cookie: str
    user_id: str

class Monitor(MonitorCreate):
    id: str
    status: str = "active"
    posts_found: int = 0
    created_at: str

def send_telegram(token: str, chat_id: str, message: str):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def scan_linkedin(keyword: str, cookie: str):
    headers = {
        "cookie": f"li_at={cookie}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "accept-language": "en-US,en;q=0.9",
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
    }
    kw_encoded = keyword.replace(" ", "%20")
    url = f"https://www.linkedin.com/voyager/api/search/hits?decorationId=com.linkedin.voyager.deco.jserp.WebSearchHit-27&count=10&origin=GLOBAL_SEARCH_HEADER&q=jserpFiltered&query=(keywords:{kw_encoded},flagshipSearchIntent:SEARCH_SRP)&start=0"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"LinkedIn scan error: {e}")
    return None

def monitor_worker(monitor_data: dict):
    monitor_id = monitor_data["id"]
    seen_posts = set()
    print(f"Monitor {monitor_id} started")

    send_telegram(
        monitor_data["telegram_token"],
        monitor_data["telegram_chat_id"],
        f"🟢 <b>LinkedAlert Active!</b>\n\nMonitor: {monitor_data['name']}\nKeywords: {', '.join(monitor_data['keywords'])}\nLocation: {monitor_data['location']}\n\nScanning every {monitor_data['interval_minutes']} minutes..."
    )

    while monitor_id in active_monitors and active_monitors[monitor_id]["running"]:
        for keyword in monitor_data["keywords"]:
            try:
                data = scan_linkedin(keyword, monitor_data["linkedin_cookie"])
                if not data:
                    continue
                for item in data.get("elements", [])[:5]:
                    post_url = item.get("navigationUrl", "")
                    uid = item.get("targetUrn", post_url)
                    uid_hash = hashlib.md5(str(uid).encode()).hexdigest()
                    if uid_hash in seen_posts:
                        continue
                    seen_posts.add(uid_hash)
                    name = item.get("headerText", {}).get("text", "Unknown")
                    title = item.get("title", {}).get("text", "No text available")[:300]
                    subtitle = item.get("primarySubtitle", {}).get("text", "")
                    msg = (
                        f"🚨 <b>New LinkedIn Post Found!</b>\n\n"
                        f"👤 <b>Name:</b> {name}\n"
                        f"💼 <b>Title:</b> {subtitle}\n"
                        f"🔍 <b>Keyword:</b> {keyword}\n"
                        f"📝 <b>Post:</b> {title}...\n"
                        f"📍 <b>Location:</b> {monitor_data['location']}\n"
                        f"⏰ <b>Time:</b> {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
                    )
                    if post_url:
                        msg += f"\n🔗 <a href='{post_url}'>View Post</a>"
                    send_telegram(monitor_data["telegram_token"], monitor_data["telegram_chat_id"], msg)
                    if supabase:
                        supabase.table("alerts").insert({
                            "monitor_id": monitor_id,
                            "user_id": monitor_data["user_id"],
                            "name": name,
                            "keyword": keyword,
                            "post_text": title,
                            "post_url": post_url,
                            "created_at": datetime.now().isoformat()
                        }).execute()
            except Exception as e:
                print(f"Scan error for keyword {keyword}: {e}")

        interval = monitor_data.get("interval_minutes", 15) * 60
        for _ in range(interval):
            if monitor_id not in active_monitors or not active_monitors[monitor_id]["running"]:
                break
            time.sleep(1)

    print(f"Monitor {monitor_id} stopped")

@app.get("/")
def root():
    return {"status": "LinkedAlert API running", "version": "1.0.0"}

@app.post("/monitors")
def create_monitor(monitor: MonitorCreate):
    monitor_id = hashlib.md5(f"{monitor.user_id}{monitor.name}{time.time()}".encode()).hexdigest()[:12]
    monitor_data = {
        **monitor.dict(),
        "id": monitor_id,
        "status": "active",
        "posts_found": 0,
        "created_at": datetime.now().isoformat()
    }
    if supabase:
        supabase.table("monitors").insert(monitor_data).execute()
    active_monitors[monitor_id] = {"running": True, "data": monitor_data}
    thread = threading.Thread(target=monitor_worker, args=(monitor_data,), daemon=True)
    thread.start()
    return {"success": True, "monitor_id": monitor_id, "message": "Monitor started! Check Telegram."}

@app.get("/monitors/{user_id}")
def get_monitors(user_id: str):
    if supabase:
        result = supabase.table("monitors").select("*").eq("user_id", user_id).execute()
        return result.data
    return []

@app.delete("/monitors/{monitor_id}")
def stop_monitor(monitor_id: str):
    if monitor_id in active_monitors:
        active_monitors[monitor_id]["running"] = False
        del active_monitors[monitor_id]
    if supabase:
        supabase.table("monitors").update({"status": "stopped"}).eq("id", monitor_id).execute()
    return {"success": True, "message": "Monitor stopped"}

@app.get("/alerts/{user_id}")
def get_alerts(user_id: str):
    if supabase:
        result = supabase.table("alerts").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(50).execute()
        return result.data
    return []

@app.get("/health")
def health():
    return {"status": "healthy", "active_monitors": len(active_monitors)}
