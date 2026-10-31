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
        # Chromium flags tailored for Render's 512MB RAM Linux container
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-blink-features=AutomationControlled",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--mute-audio",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=AudioServiceSandbox,TranslateUI",
                "--js-flags=--max-old-space-size=256"
            ]
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720},
            color_scheme="dark",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )

        # Inject session cookies
        clean_cookies = []
        for c in user_cookies:
            try:
                name_val = str(c.get("name", "")).strip()
                val = str(c.get("value", "")).strip()
                if not name_val:
                    continue

                raw_domain = str(c.get("domain", ".google.com")).strip()
                clean_domain = raw_domain if raw_domain.startswith(".") else f".{raw_domain}"

                entry = {
                    "name": name_val,
                    "value": val,
                    "domain": clean_domain,
                    "path": str(c.get("path", "/")),
                    "secure": True
                }

                same_site = c.get("sameSite")
                if isinstance(same_site, str):
                    s_lower = same_site.lower()
                    if s_lower in ["none", "no_restriction"]:
                        entry["sameSite"] = "None"
                    elif s_lower == "lax":
                        entry["sameSite"] = "Lax"
                    elif s_lower == "strict":
                        entry["sameSite"] = "Strict"

                clean_cookies.append(entry)
            except Exception:
                continue

        try:
            await context.add_cookies(clean_cookies)
            print(f"[{session_id}] Injected {len(clean_cookies)} cookies.")
        except Exception as e:
            print(f"[{session_id}] Cookie loading issue: {e}")
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
            print(f"[{session_id}] Opening {url}...")
            await page.goto(url, wait_until="networkidle", timeout=45000)

            # Wait and interact with the lobby
            joined = False
            for attempt in range(25):
                await page.screenshot(path="last_view.png")

                # Clear blocking prompt dialogs
                for text in ["Got it", "Dismiss", "I agree", "Accept all", "Continue without microphone"]:
                    dismiss_btn = page.locator(f'button:has-text("{text}")')
                    if await dismiss_btn.count() > 0:
                        try:
                            await dismiss_btn.first.click(timeout=800)
                        except Exception:
                            pass

                # Mute mic and camera via hotkeys
                try:
                    await page.keyboard.press("Control+d")
                    await page.keyboard.press("Control+e")
                except Exception:
                    pass

                # Locate the join button variants
                join_targets = [
                    'button:has-text("Ask to join")',
                    'button:has-text("Join now")',
                    'button[aria-label*="Ask to join"]',
                    'button[aria-label*="Join now"]',
                    '[data-mdc-dialog-action="join"]'
                ]

                for selector in join_targets:
                    btn = page.locator(selector)
                    if await btn.count() > 0:
                        if await btn.first.is_visible() and await btn.first.is_enabled():
                            print(f"[{session_id}] Clicking join button: {selector}")
                            await btn.first.click()
                            joined = True
                            break

                if joined:
                    break

                await asyncio.sleep(2)

            await asyncio.sleep(5)
            await page.screenshot(path="last_view.png")

            # In-meeting keep-alive loop
            while session_id in sessions and sessions[session_id]["status"] == "running":
                await page.screenshot(path="last_view.png")
                # Exit if kicked or ended
                if await page.locator('text="You left the meeting", text="You were removed"').count() > 0:
                    break
                await asyncio.sleep(10)

        except Exception as e:
            print(f"[{session_id}] Runtime error: {e}")
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
