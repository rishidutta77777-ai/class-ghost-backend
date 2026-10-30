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
                "--disable-audio-output",
                "--no-sandbox"
            ]
        )
        context = await browser.new_context(permissions=[])
        page = await context.new_page()
        sessions[session_id] = {"browser": browser, "status": "running"}

        try:
            await page.goto(url, wait_until="networkidle")
            await asyncio.sleep(2)

            name_input = page.locator('input[type="text"]')
            if await name_input.count() > 0:
                await name_input.first.fill(name)
                await asyncio.sleep(1)

            join_btn = page.locator('button:has-text("Ask to join"), button:has-text("Join now")')
            if await join_btn.count() > 0:
                await join_btn.first.click()

            while session_id in sessions and sessions[session_id]["status"] == "running":
                ended_locator = page.locator('text="You left the meeting", text="The call ended", text="Return to home screen"')
                if await ended_locator.count() > 0:
                    break
                await asyncio.sleep(5)

        except Exception as e:
            print(f"Session error: {e}")
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
        await sess["browser"].close()
    return {"status": "terminated"}

@app.get("/status")
async def get_status(session_id: str):
    sess = sessions.get(session_id, {"status": "ended"})
    return {"status": sess.get("status", "ended")}
