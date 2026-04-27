from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import requests
import time
import threading
import hashlib
import os
import json
from datetime import datetime
from supabase import create_client, Client
from contextlib import asynccontextmanager

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

active_monitors = {}

INFLUENCER_KEYWORDS = [
    'influencer', 'content creator', 'ugc', 'brand collaboration',
    'creator', 'social media', 'brand deal', 'paid promotion',
    'brand ambassador', 'sponsored', 'collab', 'collaboration',
    'influencer marketing', 'micro influencer', 'nano influencer'
]

def is_relevant_post(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in INFLUENCER_KEYWORDS)

def send_telegram(token: str, chat_id: str, message: str):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def scan_linkedin(keyword: str, cookie: str):
    headers = {
        "cookie": f"li_at={cookie}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "x-restli-protocol-version": "2.0.0",
    }
    kw = keyword.replace(" ", "%20")
    url = f"https://www.linkedin.com/voyager/api/search/hits?decorationId=com.linkedin.voyager.deco.jserp.WebSearchHit-27&count=10&q=jserpFiltered&query=(keywords:{kw},flagshipSearchIntent:SEARCH_SRP)&start=0"
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
    print(f"Monitor started: {monitor_data['name']}")
    send_telegram(
        monitor_data["telegram_token"],
        monitor_data["telegram_chat_id"],
        f"🟢 <b>LinkedAlert Active!</b>\n\nMonitor: {monitor_data['name']}\nKeywords: {', '.join(monitor_data['keywords'])}\nLocation: {monitor_data['location']}\n\nScanning every {monitor_data['interval_minutes']} minutes..."
    )
    while monitor_id in active_monitors and active_monitors[monitor_id]["running"]:
        print(f"Scanning: {monitor_data['name']}")
        for keyword in monitor_data["keywords"]:
            try:
                data = scan_linkedin(keyword, monitor_data["linkedin_cookie"])
                if not data:
                    continue
                for item in data.get("elements", [])[:5]:
                    post_url = item.get("navigationUrl", "") or item.get("linkedinUrl", "")
                    uid = hashlib.md5(str(item.get("targetUrn", post_url)).encode()).hexdigest()
                    if uid in seen_posts:
                        continue
                    seen_posts.add(uid)
                    name = item.get("headerText", {}).get("text", "Unknown")
                    title = item.get("title", {}).get("text", "")[:300]
                    subtitle = item.get("primarySubtitle", {}).get("text", "")
                    if not is_relevant_post(title):
                        continue
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
                print(f"Scan error {keyword}: {e}")
        interval = monitor_data.get("interval_minutes", 15) * 60
        for _ in range(interval):
            if monitor_id not in active_monitors or not active_monitors[monitor_id]["running"]:
                break
            time.sleep(1)
    print(f"Monitor stopped: {monitor_id}")

def restore_monitors():
    if not supabase:
        return
    try:
        print("Restoring active monitors...")
        result = supabase.table("monitors").select("*").eq("status", "active").execute()
        for monitor_data in result.data:
            monitor_id = monitor_data["id"]
            if isinstance(monitor_data.get("keywords"), str):
                monitor_data["keywords"] = json.loads(monitor_data["keywords"])
            if monitor_id not in active_monitors:
                active_monitors[monitor_id] = {"running": True, "data": monitor_data}
                thread = threading.Thread(target=monitor_worker, args=(monitor_data,), daemon=True)
                thread.start()
                print(f"Restored: {monitor_data['name']}")
    except Exception as e:
        print(f"Restore error: {e}")

@asynccontextmanager
async def lifespan(app):
    print("Starting LinkedAlert API v2...")
    t = threading.Thread(target=restore_monitors, daemon=True)
    t.start()
    yield
    print("Shutting down...")

app = FastAPI(title="LinkedAlert API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

class MonitorCreate(BaseModel):
    name: str
    keywords: List[str]
    location: str
    interval_minutes: int = 15
    telegram_token: str
    telegram_chat_id: str
    linkedin_cookie: str
    user_id: str

@app.get("/")
def root():
    return {"status": "LinkedAlert API running", "version": "2.0.0"}

@app.post("/apify-webhook")
async def apify_webhook(request: Request):
    try:
        data = await request.json()
        print(f"Webhook received: {str(data)[:200]}")

        resource = data.get("resource", {})
        if isinstance(resource, str):
            resource = json.loads(resource)

        dataset_id = resource.get("defaultDatasetId", "")
        print(f"Dataset ID: {dataset_id}")

        if not dataset_id:
            return {"status": "no dataset"}

        url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={APIFY_TOKEN}&limit=20"
        r = requests.get(url, timeout=15)
        posts = r.json()
        print(f"Posts fetched: {len(posts)}")

        if supabase:
            monitors = supabase.table("monitors").select("*").eq("status", "active").execute().data
            seen = set()
            relevant_count = 0

            for post in posts[:20]:
                author = post.get("author", {})
                name = author.get("name", "Unknown") if isinstance(author, dict) else "Unknown"
                text = post.get("text", "")[:300]
                post_url = post.get("linkedinUrl", "") or post.get("url", "")
                search_query = post.get("searchQuery", {})
                keyword = search_query.get("query", "") if isinstance(search_query, dict) else ""

                # Filter irrelevant posts
                if not is_relevant_post(text):
                    print(f"Skipping irrelevant post: {name}")
                    continue

                uid = hashlib.md5(str(post_url).encode()).hexdigest()
                if uid in seen:
                    continue
                seen.add(uid)
                relevant_count += 1

                msg = (
                    f"🚨 <b>New LinkedIn Post!</b>\n\n"
                    f"👤 <b>Name:</b> {name}\n"
                    f"🔍 <b>Keyword:</b> {keyword}\n"
                    f"📝 <b>Post:</b> {text}...\n"
                    f"⏰ <b>Time:</b> {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
                )
                if post_url:
                    msg += f"\n🔗 <a href='{post_url}'>View Post</a>"

                for monitor in monitors:
                    send_telegram(monitor["telegram_token"], monitor["telegram_chat_id"], msg)
                    print(f"Telegram sent for: {name}")

                if supabase:
                    for monitor in monitors:
                        supabase.table("alerts").insert({
                            "monitor_id": monitor["id"],
                            "user_id": monitor["user_id"],
                            "name": name,
                            "keyword": keyword,
                            "post_text": text,
                            "post_url": post_url,
                            "created_at": datetime.now().isoformat()
                        }).execute()

            print(f"Relevant posts sent: {relevant_count}")

        return {"status": "ok", "posts_processed": len(posts)}
    except Exception as e:
        print(f"Webhook error: {e}")
        import traceback
        print(traceback.format_exc())
        return {"status": "error", "message": str(e)}

@app.post("/monitors")
def create_monitor(monitor: MonitorCreate):
    monitor_id = hashlib.md5(f"{monitor.user_id}{monitor.name}{time.time()}".encode()).hexdigest()[:12]
    monitor_data = {**monitor.dict(), "id": monitor_id, "status": "active", "posts_found": 0, "created_at": datetime.now().isoformat()}
    if supabase:
        supabase.table("monitors").insert(monitor_data).execute()
    active_monitors[monitor_id] = {"running": True, "data": monitor_data}
    thread = threading.Thread(target=monitor_worker, args=(monitor_data,), daemon=True)
    thread.start()
    return {"success": True, "monitor_id": monitor_id, "message": "Monitor started!"}

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
    return {"success": True}

@app.get("/alerts/{user_id}")
def get_alerts(user_id: str):
    if supabase:
        result = supabase.table("alerts").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(50).execute()
        return result.data
    return []

@app.get("/health")
def health():
    return {"status": "healthy", "active_monitors": len(active_monitors), "version": "2.0.0"}
