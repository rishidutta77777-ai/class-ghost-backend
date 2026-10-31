import asyncio
import os
import uuid
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

class StopRequest(BaseModel):
    session_id: str

async def run_bot_lifecycle(session_id: str, url: str, name: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--enable-usermedia-screen-capturing",
                "--allow-http-screen-capture",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security"
            ]
        )
        
        # Emulate a current Chrome browser with full client hints
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720},
            extra_http_headers={
                "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"'
            }
        )
        
        page = await context.new_page()
        
        # Patch bot markers
        await page.add_init_script("""
            delete Object.getPrototypeOf(navigator).webdriver;
            window.navigator.chrome = { runtime: {} };
        """)
        
        sessions[session_id] = {"browser": browser, "page": page, "status": "running"}

        try:
            print(f"[{session_id}] Connecting to {url}...")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(4)

            await page.screenshot(path="last_view.png")

            # Dismiss common popups/banners
            for text in ["Got it", "Dismiss", "I agree", "Accept all", "Continue without microphone"]:
                btn = page.locator(f'button:has-text("{text}")')
                if await btn.count() > 0:
                    try:
                        await btn.first.click(timeout=1500)
                    except Exception:
                        pass

            # Turn off mic & camera
            await page.keyboard.press("Control+d")
            await page.keyboard.press("Control+e")
            await asyncio.sleep(1)

            # Fill name if prompt is visible
            name_input = page.locator('input[aria-label="Your name"], input[placeholder="Your name"], input[type="text"]')
            if await name_input.count() > 0:
                print(f"[{session_id}] Filling name: {name}")
                await name_input.first.click()
                await name_input.first.fill(name)
                await asyncio.sleep(1)

            # Click Join / Ask to join
            join_btn = page.locator('button:has-text("Ask to join"), button:has-text("Join now")')
            if await join_btn.count() > 0:
                print(f"[{session_id}] Clicking join...")
                await join_btn.first.click()
            
            await asyncio.sleep(4)
            await page.screenshot(path="last_view.png")

            while session_id in sessions and sessions[session_id]["status"] == "running":
                await asyncio.sleep(5)

        except Exception as e:
            print(f"[{session_id}] Session failed: {e}")
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
    background_tasks.add_task(run_bot_lifecycle, session_id, req.url, req.name)
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
