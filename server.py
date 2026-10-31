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
        # Launch Chromium with zero camera/mic capture devices and minimal memory
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-blink-features=AutomationControlled",
                "--deny-permission-prompts",
                "--mute-audio",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=AudioServiceSandbox,TranslateUI,WebRtcHideLocalIpsWithMdns",
                "--disable-canvas-aa",
                "--disable-2d-canvas-clip-aa",
                "--disable-gl-drawing-for-tests",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=128"
            ]
        )
        
        # Grant NO camera or mic permissions - keeps hardware encoders completely offline
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            permissions=[],
            viewport={"width": 800, "height": 600},
            color_scheme="dark",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )

        # Block extraneous static assets
        async def block_heavy_resources(route):
            req = route.request
            if req.resource_type in ["image", "media", "font"]:
                await route.abort()
            else:
                await route.continue_()

        # Sanitize and inject cookies
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
            print(f"[{session_id}] Cookie load error: {e}")
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

        await page.route("**/*", block_heavy_resources)
        sessions[session_id] = {"browser": browser, "page": page, "status": "running"}

        try:
            print(f"[{session_id}] Connecting to meeting...")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Wait and interact with the lobby
            joined = False
            for _ in range(25):
                # Dismiss modals and alerts
                for text in ["Got it", "Dismiss", "I agree", "Accept all", "Continue without microphone", "Continue without camera"]:
                    btn = page.locator(f'button:has-text("{text}")')
                    if await btn.count() > 0:
                        try:
                            await btn.first.click(timeout=800)
                        except Exception:
                            pass

                # Locate join buttons
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
                            print(f"[{session_id}] Clicking join: {selector}")
                            await btn.first.click()
                            joined = True
                            break

                if joined:
                    break

                await asyncio.sleep(2)

            # Wait 8s for admission into meeting room
            await asyncio.sleep(8)

            # Mute incoming audio and disable rendering inside DOM to protect RAM
            try:
                await page.evaluate("""
                    () => {
                        document.querySelectorAll('audio, video').forEach(el => {
                            el.muted = true;
                            el.pause();
                            el.srcObject = null;
                        });
                    }
                """)
            except Exception:
                pass

            # Capture single entry confirmation screenshot
            try:
                await page.screenshot(path="last_view.png")
            except Exception:
                pass

            gc.collect()

            # Ultralight idle loop: keeps session active without consuming CPU/RAM
            while session_id in sessions and sessions[session_id]["status"] == "running":
                try:
                    evicted = await page.locator('text="You left the meeting", text="You were removed"').count()
                    if evicted > 0:
                        print(f"[{session_id}] Meeting ended or bot removed.")
                        break
                except Exception:
                    pass

                await asyncio.sleep(20)

        except Exception as e:
            print(f"[{session_id}] Session encountered error: {e}")
        finally:
            await browser.close()
            if session_id in sessions:
                sessions[session_id]["status"] = "ended"
            gc.collect()

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
