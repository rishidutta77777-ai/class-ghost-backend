import asyncio
import os
import urllib.request
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class JoinRequest(BaseModel):
    meeting_url: str
    cookies: list

bot_state = {
    "playwright": None,
    "browser": None,
    "context": None,
    "page": None,
    "is_running": False,
    "anti_idle_task": None,
    "codespace_keepalive_task": None
}

async def codespace_keepalive_loop():
    """Pings the public tunnel every 2 minutes so Codespaces does not suspend the VM."""
    while bot_state["is_running"]:
        await asyncio.sleep(120)
        try:
            # Self-ping the local backend port to generate tunnel and process activity
            req = urllib.request.Request("http://127.0.0.1:8000/health", headers={"User-Agent": "KeepAlive"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

async def meet_anti_idle_loop():
    """Interacts with Google Meet so the session is never kicked for inactivity."""
    while bot_state["is_running"]:
        await asyncio.sleep(45)
        try:
            page = bot_state.get("page")
            if page and not page.is_closed():
                # 1. Micro-interaction: nudge mouse cursor and send keypress
                await page.mouse.move(100, 100)
                await page.keyboard.press("Shift")
                
                # 2. Automatically dismiss 'Are you still there?' or 'Return to call' popups
                dismiss_locators = [
                    "button:has-text('Yes')",
                    "button:has-text('Stay in call')",
                    "button:has-text('Rejoin')",
                    "button:has-text('Return to call')",
                    "div[role='button']:has-text('Continue')"
                ]
                for selector in dismiss_locators:
                    btn = page.locator(selector)
                    if await btn.count() > 0:
                        await btn.first.click()
                        break
        except Exception:
            pass

@app.get("/health")
async def health():
    return {"status": "ok", "bot_running": bot_state["is_running"]}

@app.get("/view")
async def view_snapshot():
    page = bot_state.get("page")
    if not page or page.is_closed():
        raise HTTPException(status_code=400, detail="Bot is not active")
    
    try:
        # Capture raw frame buffer directly to memory without disk cache
        img_bytes = await page.screenshot(type="jpeg", quality=65)
        return Response(
            content=img_bytes,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/start")
async def start_bot(req: JoinRequest):
    if bot_state["is_running"]:
        return {"status": "already_running"}

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720}
        )
        
        formatted_cookies = []
        for c in req.cookies:
            cookie = {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain", ".google.com"),
                "path": c.get("path", "/")
            }
            if "secure" in c:
                cookie["secure"] = c["secure"]
            formatted_cookies.append(cookie)

        await context.add_cookies(formatted_cookies)
        page = await context.new_page()

        await page.goto(req.meeting_url, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(4)

        # Mute audio & video
        try:
            mic = page.locator("div[role='button'][aria-label*='microphone' i], div[role='button'][data-is-muted]")
            if await mic.count() > 0:
                await mic.first.click()
        except Exception:
            pass

        try:
            cam = page.locator("div[role='button'][aria-label*='camera' i]")
            if await cam.count() > 0:
                await cam.first.click()
        except Exception:
            pass

        # Join room
        join_btn = page.locator("button:has-text('Ask to join'), button:has-text('Join now'), span:has-text('Ask to join'), span:has-text('Join now')")
        if await join_btn.count() > 0:
            await join_btn.first.click()

        bot_state["playwright"] = pw
        bot_state["browser"] = browser
        bot_state["context"] = context
        bot_state["page"] = page
        bot_state["is_running"] = True
        
        # Start persistent background workers
        bot_state["anti_idle_task"] = asyncio.create_task(meet_anti_idle_loop())
        bot_state["codespace_keepalive_task"] = asyncio.create_task(codespace_keepalive_loop())

        return {"status": "joined"}
    except Exception as e:
        bot_state["is_running"] = False
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stop")
async def stop_bot():
    bot_state["is_running"] = False
    
    if bot_state.get("anti_idle_task"):
        bot_state["anti_idle_task"].cancel()
    if bot_state.get("codespace_keepalive_task"):
        bot_state["codespace_keepalive_task"].cancel()

    try:
        if bot_state.get("page") and not bot_state["page"].is_closed():
            leave_btn = bot_state["page"].locator("button[aria-label*='Leave' i]")
            if await leave_btn.count() > 0:
                await leave_btn.first.click()
                await asyncio.sleep(1)
            await bot_state["page"].close()
        
        if bot_state.get("browser"):
            await bot_state["browser"].close()
        if bot_state.get("playwright"):
            await bot_state["playwright"].stop()
    except Exception:
        pass
    finally:
        os.system("gh codespace stop -c $CODESPACE_NAME &")

    return {"status": "stopped"}
