import asyncio
import os
import uuid
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
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
    name: str
    cookies: Optional[List[Dict[str, Any]]] = None

class StopRequest(BaseModel):
    session_id: str

async def run_bot_lifecycle(session_id: str, url: str, name: str, user_cookies: Optional[List[Dict[str, Any]]] = None):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security"
            ]
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720},
            color_scheme="dark",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )

        # Inject user-specific cookies if provided
        if user_cookies:
            clean_cookies = []
            for c in user_cookies:
                cookie_entry = {
                    "name": c.get("name"),
                    "value": c.get("value"),
                    "domain": c.get("domain", ".google.com"),
                    "path": c.get("path", "/")
                }
                if "sameSite" in c and c["sameSite"] in ["Strict", "Lax", "None"]:
                    cookie_entry["sameSite"] = c["sameSite"]
                clean_cookies.append(cookie_entry)
            try:
                await context.add_cookies(clean_cookies)
                print(f"[{session_id}] Injected {len(clean_cookies)} user cookies.")
            except Exception as e:
                print(f"[{session_id}] Cookie injection error: {e}")

        page = await context.new_page()

        # Mask automation indicators
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        sessions[session_id] = {"browser": browser, "page": page, "status": "running"}

        try:
            print(f"[{session_id}] Connecting to {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Wait for lobby/pre-call interface
            for _ in range(12):
                await page.screenshot(path="last_view.png")
                if await page.locator('text="You can\'t join this video call"').count() > 0:
                    print(f"[{session_id}] Blocked: Meeting requires Google sign-in.")
                    return
                if await page.locator('input[type="text"], button:has-text("Ask to join"), button:has-text("Join now")').count() > 0:
                    break
                await asyncio.sleep(2)

            # Dismiss pre-join permission modals
            for text in ["Got it", "Dismiss", "I agree", "Accept all", "Continue without microphone"]:
                btn = page.locator(f'button:has-text("{text}")')
                if await btn.count() > 0:
                    try:
                        await btn.first.click(timeout=1500)
                    except Exception:
                        pass

            # Mute microphone and camera
            try:
                await page.keyboard.press("Control+d")
                await page.keyboard.press("Control+e")
            except Exception:
                pass
            await asyncio.sleep(1)

            # Guest Name Field (Only appears if no cookies / guest mode)
            name_input = page.locator('input[aria-label="Your name"], input[placeholder="Your name"], input[type="text"]')
            if await name_input.count() > 0:
                print(f"[{session_id}] Typing guest name: {name}")
                await name_input.first.click()
                await name_input.first.fill(name)
                await asyncio.sleep(1)

            await page.screenshot(path="last_view.png")

            # Click Join / Ask to join
            join_btn = page.locator('button:has-text("Ask to join"), button:has-text("Join now"), button[aria-label*="Ask to join"]')
            if await join_btn.count() > 0:
                print(f"[{session_id}] Clicking join...")
                await join_btn.first.click()
            else:
                print(f"[{session_id}] Join button not located.")

            await asyncio.sleep(4)
            await page.screenshot(path="last_view.png")

            while session_id in sessions and sessions[session_id]["status"] == "running":
                if await page.locator('text="You left the meeting", text="You can\'t join this video call"').count() > 0:
                    break
                await asyncio.sleep(5)

        except Exception as e:
            print(f"[{session_id}] Error: {e}")
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
