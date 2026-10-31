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

        # Standardize Cookie-Editor exports for Chromium
        clean_cookies = []
        for c in user_cookies:
            try:
                name_val = str(c.get("name", ""))
                val = str(c.get("value", ""))
                if not name_val:
                    continue

                raw_domain = str(c.get("domain", ".google.com"))
                # Strip leading dot if present for standard format
                clean_domain = raw_domain if raw_domain.startswith(".") else f".{raw_domain}"

                cookie_entry = {
                    "name": name_val,
                    "value": val,
                    "domain": clean_domain,
                    "path": str(c.get("path", "/"))
                }

                # Normalize sameSite
                same_site = c.get("sameSite")
                if isinstance(same_site, str):
                    s_lower = same_site.lower()
                    if s_lower == "no_restriction" or s_lower == "none":
                        cookie_entry["sameSite"] = "None"
                        cookie_entry["secure"] = True
                    elif s_lower == "lax":
                        cookie_entry["sameSite"] = "Lax"
                    elif s_lower == "strict":
                        cookie_entry["sameSite"] = "Strict"

                clean_cookies.append(cookie_entry)
            except Exception:
                continue

        # Inject into ephemeral memory
        try:
            await context.add_cookies(clean_cookies)
            print(f"[{session_id}] Successfully loaded {len(clean_cookies)} cookies into session.")
        except Exception as e:
            print(f"[{session_id}] Cookie injection error: {e}")
        finally:
            del user_cookies
            del clean_cookies
            gc.collect()

        page = await context.new_page()

        # Stealth patches
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)

        sessions[session_id] = {"browser": browser, "page": page, "status": "running"}

        try:
            print(f"[{session_id}] Navigating to {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Wait up to 60 seconds for lobby elements
            for _ in range(30):
                await page.screenshot(path="last_view.png")
                # Look for join buttons
                if await page.locator('button:has-text("Ask to join"), button:has-text("Join now")').count() > 0:
                    break
                await asyncio.sleep(2)

            # Dismiss prompts / popups
            for text in ["Got it", "Dismiss", "I agree", "Accept all", "Continue without microphone"]:
                btn = page.locator(f'button:has-text("{text}")')
                if await btn.count() > 0:
                    try:
                        await btn.first.click(timeout=1500)
                    except Exception:
                        pass

            # Mute mic and cam
            try:
                await page.keyboard.press("Control+d")
                await page.keyboard.press("Control+e")
            except Exception:
                pass
            await asyncio.sleep(1)

            # Click Join / Ask to join
            join_btn = page.locator('button:has-text("Ask to join"), button:has-text("Join now"), button[aria-label*="Ask to join"]')
            if await join_btn.count() > 0:
                print(f"[{session_id}] Clicking Join button...")
                await join_btn.first.click()
            
            await asyncio.sleep(4)
            await page.screenshot(path="last_view.png")

            # Keep alive loop during meeting
            while session_id in sessions and sessions[session_id]["status"] == "running":
                await page.screenshot(path="last_view.png")
                if await page.locator('text="You left the meeting", text="You can\'t join this video call"').count() > 0:
                    break
                await asyncio.sleep(10)

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
