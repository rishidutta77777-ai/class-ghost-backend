import asyncio
import uuid
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
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
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        
        # Emulate a real desktop Chrome user agent
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720}
        )
        
        page = await context.new_page()
        sessions[session_id] = {"browser": browser, "status": "running"}

        try:
            print(f"[{session_id}] Navigating to {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)

            # Dismiss cookie/consent dialogues if they appear
            for dismiss_text in ["Accept all", "I agree", "Got it", "Dismiss"]:
                btn = page.locator(f'button:has-text("{dismiss_text}")')
                if await btn.count() > 0:
                    try:
                        await btn.first.click(timeout=2000)
                    except Exception:
                        pass

            # Fill name if prompt is visible
            name_selectors = [
                'input[aria-label="Your name"]',
                'input[placeholder="Your name"]',
                'input[type="text"]'
            ]
            
            for selector in name_selectors:
                field = page.locator(selector)
                if await field.count() > 0:
                    print(f"[{session_id}] Entering name...")
                    await field.first.fill(name)
                    await asyncio.sleep(1)
                    break

            # Click Join / Ask to join
            join_selectors = [
                'button:has-text("Ask to join")',
                'button:has-text("Join now")',
                'button[aria-label*="Ask to join"]',
                'button[aria-label*="Join now"]'
            ]
            
            clicked = False
            for selector in join_selectors:
                btn = page.locator(selector)
                if await btn.count() > 0:
                    print(f"[{session_id}] Clicking join button...")
                    await btn.first.click()
                    clicked = True
                    break

            if not clicked:
                print(f"[{session_id}] Join button not found. Current URL: {page.url}")

            # Keep alive until session terminated or call ends
            while session_id in sessions and sessions[session_id]["status"] == "running":
                ended_locator = page.locator('text="You left the meeting", text="The call ended", text="Return to home screen"')
                if await ended_locator.count() > 0:
                    print(f"[{session_id}] Meeting ended by host.")
                    break
                await asyncio.sleep(5)

        except Exception as e:
            print(f"[{session_id}] Session error: {e}")
        finally:
            await browser.close()
            if session_id in sessions:
                sessions[session_id]["status"] = "ended"

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
