import asyncio
import os
import uuid
import gc
import subprocess
import traceback
from typing import List, Dict, Any
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright

app = FastAPI(title="Class Ghost Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

sessions: Dict[str, Dict[str, Any]] = {}

class MeetingRequest(BaseModel):
    url: str
    name: str = "Authenticated Bot"
    cookies: List[Dict[str, Any]] = Field(..., min_items=1)

class StopRequest(BaseModel):
    session_id: str


def trigger_codespace_shutdown():
    """If running inside GitHub Codespaces, stop the VM automatically to save free hours."""
    if os.environ.get("CODESPACES") == "true":
        print("[System] Detected GitHub Codespace. Initiating auto-shutdown in 3 seconds...")
        asyncio.create_task(_async_codespace_stop())


async def _async_codespace_stop():
    await asyncio.sleep(3)
    codespace_name = os.environ.get("CODESPACE_NAME", "")
    try:
        if codespace_name:
            subprocess.run(["gh", "codespace", "stop", "-c", codespace_name], check=False)
        else:
            subprocess.run(["gh", "codespace", "stop"], check=False)
    except Exception as e:
        print(f"[System] Codespace stop error: {e}")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/view")
async def view_screen():
    if os.path.exists("last_view.png"):
        return FileResponse(
            "last_view.png", 
            media_type="image/png",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )
    return Response(content="No snapshot captured yet", media_type="text/plain")


@app.get("/status")
async def get_status(session_id: str):
    sess = sessions.get(session_id)
    if not sess:
        return {"status": "ended"}
    return {"status": sess.get("status", "ended")}


@app.post("/stop")
async def stop_session(req: StopRequest):
    sess = sessions.get(req.session_id)
    if sess and "browser" in sess:
        sess["status"] = "ended"
        try:
            await sess["browser"].close()
        except Exception:
            pass
    trigger_codespace_shutdown()
    return {"status": "terminated"}


async def run_bot_worker(session_id: str, url: str, raw_cookies: List[Dict[str, Any]]):
    print(f"[{session_id}] Starting runner worker for: {url}")
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-blink-features=AutomationControlled",
                    "--window-size=1280,720",
                    "--mute-audio",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-features=AudioServiceSandbox,TranslateUI,WebRtcHideLocalIpsWithMdns",
                    "--renderer-process-limit=1",
                    "--js-flags=--max-old-space-size=96"
                ]
            )

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
                device_scale_factor=1,
                locale="en-US",
                timezone_id="Asia/Kolkata",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"'
                }
            )

            # Sanitize and load cookies
            valid_cookies = []
            for c in raw_cookies:
                try:
                    name_val = str(c.get("name", "")).strip()
                    val = str(c.get("value", "")).strip()
                    if not name_val or not val:
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

                    valid_cookies.append(entry)
                except Exception:
                    continue

            if valid_cookies:
                try:
                    await context.add_cookies(valid_cookies)
                    print(f"[{session_id}] Injected {len(valid_cookies)} cookies.")
                except Exception as ce:
                    print(f"[{session_id}] Cookie inject error: {ce}")

            del raw_cookies
            del valid_cookies
            gc.collect()

            page = await context.new_page()

            # Anti-detection stealth & WebRTC suppression
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
                Object.defineProperty(window, 'outerWidth', { get: () => 1280 });
                Object.defineProperty(window, 'outerHeight', { get: () => 720 });
                if (window.RTCPeerConnection) {
                    RTCPeerConnection.prototype.addTrack = function() { return null; };
                }
            """)

            sessions[session_id] = {"browser": browser, "page": page, "status": "running"}

            print(f"[{session_id}] Opening {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Lobby navigation
            joined = False
            for _ in range(30):
                try:
                    await page.screenshot(path="last_view.png")
                except Exception:
                    pass

                for text in ["Got it", "Dismiss", "I agree", "Accept all", "Continue without microphone", "Continue without camera"]:
                    dismiss = page.locator(f'button:has-text("{text}")')
                    if await dismiss.count() > 0:
                        try:
                            await dismiss.first.click(timeout=800)
                        except Exception:
                            pass

                try:
                    await page.keyboard.press("Control+d")
                    await page.keyboard.press("Control+e")
                except Exception:
                    pass

                for selector in [
                    'button:has-text("Ask to join")',
                    'button:has-text("Join now")',
                    'button[aria-label*="Ask to join"]',
                    'button[aria-label*="Join now"]'
                ]:
                    btn = page.locator(selector)
                    if await btn.count() > 0:
                        if await btn.first.is_visible() and await btn.first.is_enabled():
                            print(f"[{session_id}] Clicking join...")
                            await asyncio.sleep(1)
                            await btn.first.click()
                            joined = True
                            break

                if joined:
                    break
                await asyncio.sleep(2)

            await asyncio.sleep(8)

            try:
                await page.evaluate("""
                    () => {
                        document.querySelectorAll('audio, video').forEach(el => {
                            el.muted = true;
                            el.pause();
                            if (el.srcObject) {
                                el.srcObject.getTracks().forEach(t => t.stop());
                                el.srcObject = null;
                            }
                        });
                    }
                """)
            except Exception:
                pass

            try:
                await page.screenshot(path="last_view.png")
            except Exception:
                pass

            gc.collect()

            # Keep-alive loop
            while session_id in sessions and sessions[session_id]["status"] == "running":
                try:
                    evicted = await page.locator('text="You left the meeting", text="You were removed"').count()
                    if evicted > 0:
                        print(f"[{session_id}] Meeting ended by host.")
                        break
                except Exception:
                    pass

                await asyncio.sleep(20)
                gc.collect()

    except Exception as e:
        print(f"[{session_id}] Runner error: {e}")
        traceback.print_exc()
    finally:
        print(f"[{session_id}] Cleaning up session.")
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if session_id in sessions:
            sessions[session_id]["status"] = "ended"
        gc.collect()
        trigger_codespace_shutdown()


@app.post("/start")
async def start_session(req: MeetingRequest, background_tasks: BackgroundTasks):
    session_id = str(uuid.uuid4())
    sessions[session_id] = {"status": "starting"}
    background_tasks.add_task(run_bot_worker, session_id, req.url, req.cookies)
    return {"session_id": session_id}
