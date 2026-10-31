import asyncio
import os
import uuid
import gc
from typing import List, Dict, Any
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

sessions = {}

class MeetingRequest(BaseModel):
    url: str
    name: str = "Authenticated Bot"
    cookies: List[Dict[str, Any]] = Field(..., min_items=1)

class StopRequest(BaseModel):
    session_id: str

async def run_bot_lifecycle(session_id: str, url: str, name: str, user_cookies: List[Dict[str, Any]]):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--disable-gpu"
            ]
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720},
            color_scheme="dark",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )

        # Sanitize and inject cookies one-by-one so Chromium accepts them without error
        injected_count = 0
        for c in user_cookies:
            try:
                name_val = str(c.get("name", "")).strip()
                val = str(c.get("value", "")).strip()
                if not name_val:
                    continue

                raw_domain = str(c.get("domain", ".google.com")).strip()
                clean_domain = raw_domain if raw_domain.startswith(".") else f".{raw_domain}"

                cookie_entry = {
                    "name": name_val,
                    "value": val,
                    "domain": clean_domain,
                    "path": str(c.get("path", "/"))
                }

                # Force secure=True for all Google session tokens
                is_secure = c.get("secure", False)
                if name_val.startswith("__Secure-") or name_val.startswith("__Host-") or "google" in clean_domain:
                    is_secure = True
                cookie_entry["secure"] = is_secure

                if "httpOnly" in c:
                    cookie_entry["httpOnly"] = bool(c["httpOnly"])

                same_site = c.get("sameSite")
                if isinstance(same_site, str):
                    s_lower = same_site.lower()
                    if s_lower in ["none", "no_restriction"]:
                        cookie_entry["sameSite"] = "None"
                        cookie_entry["secure"] = True
                    elif s_lower == "lax":
                        cookie_entry["sameSite"] = "Lax"
                    elif s_lower == "strict":
                        cookie_entry["sameSite"] = "Strict"

                await context.add_cookies([cookie_entry])
                injected_count += 1
            except Exception:
                continue

        print(f"[{session_id}] Injected {injected_count} cookies into session.")
        del user_cookies
        gc.collect()

        page = await context.new_page()

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        sessions[session_id] = {"browser": browser, "page": page, "status": "running"}

        try:
            print(f"[{session_id}] Loading meeting URL...")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Continuously dismiss blocking modals while waiting for lobby to ready
            for _ in range(40):
                await page.screenshot(path="last_view.png")

                # Dismiss common Google popups/overlays
                for text in ["Got it", "Dismiss", "I agree", "Accept all", "Continue without microphone"]:
                    btn = page.locator(f'button:has-text("{text}")')
                    if await btn.count() > 0:
                        try:
                            await btn.first.click(timeout=1000)
                        except Exception:
                            pass

                # Mute mic and cam
                try:
                    await page.keyboard.press("Control+d")
                    await page.keyboard.press("Control+e")
                except Exception:
                    pass

                # Check if Join or Ask to Join is available
                join_btn = page.locator('button:has-text("Ask to join"), button:has-text("Join now"), button[aria-label*="Ask to join"]')
                if await join_btn.count() > 0:
                    print(f"[{session_id}] Join button found! Clicking...")
                    await join_btn.first.click()
                    break

                await asyncio.sleep(2)

            await asyncio.sleep(5)
            await page.screenshot(path="last_view.png")

            # Meeting monitor loop
            while session_id in sessions and sessions[session_id]["status"] == "running":
                await page.screenshot(path="last_view.png")
                # Detect actual end of meeting or eviction
                if await page.locator('text="You left the meeting", text="You were removed"').count() > 0:
                    break
                await asyncio.sleep(8)

        except Exception as e:
            print(f"[{session_id}] Encountered runtime error: {e}")
        finally:
            await browser.close()
            if session_id in sessions:
                sessions[session_id]["status"] = "ended"

@app.get("/view")
async def view_screen():
    if os.path.exists("last_view.png"):
        return FileResponse("last_view.png", media_type="image/png")
    return Response(content="No snapshot captured yet", media_type="text/plain")

@app.post("/start")
async def start_session(req: MeetingRequest, background_tasks: BackgroundTasks):
    session_id = str(uuid.uuid4())
    sessions[session_id] = {"status": "starting"}
    background_tasks.add_task(run_bot_lifecycle, session_id, req.url, req.name, req.cookies)
    return {"session_id": session_id}

@app.post("/stop")
async def stop_session(req: StopRequest):
    sess = sessions.get(req.session_id)
    if sess and "browser" in sess:
        sess["status"] = "ended"
        try:
            await sess["browser"].close()
        except Exception:
            pass
    return {"status": "terminated"}

@app.get("/status")
async def get_status(session_id: str):
    sess = sessions.get(session_id, {"status": "ended"})
    return {"status": sess.get("status", "ended")}
