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
    "meeting_url": "",
    "watchdog_task": None,
    "keepalive_task": None
}

async def watchdog_and_anti_kick_loop():
    """Continuously monitors call state, generates real activity, and auto-rejoins if disconnected."""
    while bot_state["is_running"]:
        await asyncio.sleep(20)
        page = bot_state.get("page")
        if not page or page.is_closed():
            continue

        try:
            curr_url = page.url

            # 1. Check if Google Meet kicked the bot to a landing or post-call page
            if "meet.google.com" in curr_url and not any(code in curr_url for code in bot_state["meeting_url"].split("/")[-1:]):
                # If diverted away from the meeting room, force reload into the room
                await page.goto(bot_state["meeting_url"], wait_until="networkidle", timeout=30000)
                await asyncio.sleep(3)
                join_btn = page.locator("button:has-text('Ask to join'), button:has-text('Join now'), span:has-text('Join now')")
                if await join_btn.count() > 0:
                    await join_btn.first.click()
                continue

            # 2. Click any modal dismiss buttons ('Still there?', 'Rejoin', 'Return to call')
            dismiss_selectors = [
                "button:has-text('Rejoin')",
                "button:has-text('Return to call')",
                "button:has-text('Stay in call')",
                "button:has-text('Yes')",
                "div[role='button']:has-text('Continue')"
            ]
            for sel in dismiss_selectors:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click()
                    await asyncio.sleep(1)

            # 3. Simulate human interaction (DOM cursor movement and focus event)
            await page.mouse.move(200, 200)
            await page.evaluate("window.dispatchEvent(new Event('mousemove')); window.dispatchEvent(new Event('focus'));")

        except Exception:
            pass

async def codespace_keepalive():
    """Keeps Codespace VM network tunnel active so GitHub does not suspend the machine."""
    while bot_state["is_running"]:
        await asyncio.sleep(120)
        try:
            req = urllib.request.Request("http://127.0.0.1:8000/health", headers={"User-Agent": "WatchdogKeepAlive"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

@app.get("/health")
async def health():
    in_call = False
    page = bot_state.get("page")
    if page and not page.is_closed() and bot_state["is_running"]:
        in_call = True
    return {"status": "ok", "bot_running": in_call}

@app.get("/view")
async def view_snapshot():
    page = bot_state.get("page")
    if not page or page.is_closed():
        raise HTTPException(status_code=400, detail="Bot is not active")
    
    try:
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
        bot_state["meeting_url"] = req.meeting_url
        pw = await async_playwright().start()
        
        # Chromium launched with fake synthetic audio streaming hardware
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
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

        # Mute camera
        try:
            cam = page.locator("div[role='button'][aria-label*='camera' i]")
            if await cam.count() > 0:
                await cam.first.click()
        except Exception:
            pass

        # Ensure mic button is toggled to generate media pipeline connection
        try:
            mic = page.locator("div[role='button'][aria-label*='microphone' i]")
            if await mic.count() > 0:
                await mic.first.click()
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
        
        # Start persistent Watchdog and VM keepalive
        bot_state["watchdog_task"] = asyncio.create_task(watchdog_and_anti_kick_loop())
        bot_state["keepalive_task"] = asyncio.create_task(codespace_keepalive())

        return {"status": "joined"}
    except Exception as e:
        bot_state["is_running"] = False
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stop")
async def stop_bot():
    bot_state["is_running"] = False
    
    if bot_state.get("watchdog_task"):
        bot_state["watchdog_task"].cancel()
    if bot_state.get("keepalive_task"):
        bot_state["keepalive_task"].cancel()

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
