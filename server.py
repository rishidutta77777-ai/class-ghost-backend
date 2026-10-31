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
    # Cookies are mandatory and must be a non-empty list
    cookies: List[Dict[str, Any]] = Field(..., min_items=1)

class StopRequest(BaseModel):
    session_id: str

async def run_bot_lifecycle(session_id: str, url: str, name: str, user_cookies: List[Dict[str, Any]]):
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

        # Sanitize and format cookies into memory
        clean_cookies = []
        for c in user_cookies:
            cookie_entry = {
                "name": str(c.get("name")),
                "value": str(c.get("value")),
                "domain": str(c.get("domain", ".google.com")),
                "path": str(c.get("path", "/"))
            }
            if "sameSite" in c and c["sameSite"] in ["Strict", "Lax", "None"]:
                cookie_entry["sameSite"] = c["sameSite"]
            clean_cookies.append(cookie_entry)

        # Inject into ephemeral browser memory
        try:
            await context.add_cookies(clean_cookies)
            print(f"[{session_id}] Injected {len(clean_cookies)} session cookies successfully.")
        except Exception as e:
            print(f"[{session_id}] Cookie injection error: {e}")
        finally:
            # Wipe plain references from RAM immediately
            del user_cookies
            del clean_cookies
            gc.collect()

        page = await context.new_page()

        # Stealth property patches
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        sessions[session_id] = {"browser": browser, "page": page, "status": "running"}

        try:
            print(f"[{session_id}] Navigating to {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Wait for lobby elements
            for _ in range(12):
                await page.screenshot(path="last_view.png")
                if await page.locator('button:has-text("Ask to join"), button:has-text("Join now")').count() > 0:
                    break
                await asyncio.sleep(2)

            # Dismiss common Google modals
            for text in ["Got it", "Dismiss", "I agree", "Accept all", "Continue without microphone"]:
                btn = page.locator(f'button:has-text("{text}")')
                if await btn.count() > 0:
                    try:
                        await btn.first.click(timeout=1500)
                    except Exception:
                        pass

            # Mute microphone and camera shortcuts
            try:
                await page.keyboard.press("Control+d")
                await page.keyboard.press("Control+e")
            except Exception:
                pass
            await asyncio.sleep(1)

            # Click Join / Ask to Join
            join_btn = page.locator('button:has-text("Ask to join"), button:has-text("Join now"), button[aria-label*="Ask to join"]')
            if await join_btn.count() > 0:
                print(f"[{session_id}] Clicking Join button...")
                await join_btn.first.click()
            else:
                print(f"[{session_id}] Join button not ready or not found.")

            await asyncio.sleep(4)
            await page.screenshot(path="last_view.png")

            # Maintain active call loop
            while session_id in sessions and sessions[session_id]["status"] == "running":
                if await page.locator('text="You left the meeting", text="You can\'t join this video call"').count() > 0:
                    break
                await asyncio.sleep(5)

        except Exception as e:
            print(f"[{session_id}] Session encountered error: {e}")
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
    if not req.cookies:
        raise HTTPException(status_code=400, detail="Google session cookies are mandatory.")
    
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
