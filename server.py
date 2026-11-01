import os
import gc
import asyncio
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

app = Flask(__name__)
CORS(app)

# Global Bot State (Strictly 1 browser instance allowed to protect 512MB RAM)
BOT_STATE = {
    "status": "idle",       # idle | starting | in_class | leaving | error
    "message": "Ready to deploy.",
    "participants": 0,
    "meet_url": "",
    "student_name": "",
    "stop_requested": False
}

STATE_LOCK = threading.Lock()

# Spoofs real Windows Laptop Hardware & strips automation flags
STEALTH_PATCH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 4 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en-US', 'en'] });
window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };

const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
  if (param === 37445) return 'Intel Inc.';
  if (param === 37446) return 'Intel(R) Iris(R) Xe Graphics';
  return getParameter.apply(this, arguments);
};
"""

def make_anti_spoof_name(raw_name: str) -> str:
    """
    Inserts an invisible Unicode Zero-Width Non-Joiner (\u200c) inside the name.
    To human eyes on the teacher's screen it reads identically,
    but it prevents Google Meet from matching the string against the host's
    Google Contacts and triggering the 'With potential risks' impersonation warning.
    """
    clean = raw_name.strip()
    if len(clean) > 2:
        return clean[:2] + "\u200c" + clean[2:]
    return clean

async def meet_bot_worker(meet_url: str, raw_name: str, max_minutes: int, min_participants: int):
    with STATE_LOCK:
        BOT_STATE["status"] = "starting"
        BOT_STATE["message"] = "Booting ultra-low-RAM stealth browser..."
        BOT_STATE["participants"] = 0
        BOT_STATE["stop_requested"] = False

    safe_name = make_anti_spoof_name(raw_name)
    browser = None
    context = None

    try:
        async with async_playwright() as p:
            # Launch Chromium using '--headless=new' (True Chrome engine without Xvfb RAM overhead)
            # Tuned specifically for 512MB RAM & 0.1 CPU cloud containers
            browser = await p.chromium.launch(
                headless=True,
                ignore_default_args=["--enable-automation"],
                args=[
                    "--headless=new",
                    "--disable-blink-features=AutomationControlled",
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--mute-audio",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-translate",
                    "--no-first-run",
                    "--renderer-process-limit=1",
                    "--js-flags=--max-old-space-size=192",
                    "--window-size=1280,720"
                ]
            )

            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                permissions=["microphone", "camera"]
            )

            await context.add_init_script(STEALTH_PATCH_JS)
            page = await context.new_page()
            await stealth_async(page)

            # Block heavy fonts & non-essential images to save ~120MB RAM and 0.1 CPU cycles
            async def block_heavy_assets(route):
                if route.request.resource_type in ["font", "media"]:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", block_heavy_assets)

            with STATE_LOCK:
                BOT_STATE["message"] = "Opening Google Meet lobby..."

            # Extended timeout (90s) because 0.1 CPU throttles TLS handshakes and JS parsing
            await page.goto(meet_url, wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(10)

            # Hardware mute Mic (Ctrl+D) and Cam (Ctrl+E)
            await page.keyboard.press("Control+d")
            await asyncio.sleep(1)
            await page.keyboard.press("Control+e")
            await asyncio.sleep(2)

            # Dismiss any 'Continue without microphone/camera' popups if shown
            for dismiss_text in ["Continue without microphone and camera", "Got it", "Dismiss"]:
                try:
                    d_btn = page.locator(f"button:has-text('{dismiss_text}')").first
                    if await d_btn.is_visible():
                        await d_btn.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

            # Type Custom Name with human keystroke pacing
            with STATE_LOCK:
                BOT_STATE["message"] = f"Entering name: {raw_name}..."

            try:
                name_input = page.locator("input[type='text'], input[aria-label*='name' i]").first
                await name_input.wait_for(state="visible", timeout=15000)
                await name_input.click()
                await asyncio.sleep(0.5)
                await name_input.fill("")
                await name_input.type(safe_name, delay=90)
                await asyncio.sleep(1.5)
            except Exception as e:
                print(f"[!] Name input note: {e}")

            # Click 'Ask to join' or 'Join now'
            joined_clicked = False
            for btn_label in ["Ask to join", "Join now", "Switch here"]:
                try:
                    btn = page.locator(f"button:has-text('{btn_label}')").first
                    if await btn.is_visible():
                        await btn.click()
                        joined_clicked = True
                        break
                except Exception:
                    pass

            if not joined_clicked:
                with STATE_LOCK:
                    BOT_STATE["status"] = "error"
                    BOT_STATE["message"] = "Could not find Join button (Check link or meeting status)."
                return

            with STATE_LOCK:
                BOT_STATE["status"] = "in_class"
                BOT_STATE["message"] = "Knocking / Sitting in class (Cam & Mic OFF)."

            # Main monitoring loop (Checks every 10s for stop signal, teacher ending call, or student drop)
            elapsed = 0
            max_seconds = max_minutes * 60
            peak_participants = 0

            while elapsed < max_seconds:
                with STATE_LOCK:
                    if BOT_STATE["stop_requested"]:
                        BOT_STATE["status"] = "leaving"
                        BOT_STATE["message"] = "Emergency Leave triggered by user."
                        break

                await asyncio.sleep(10)
                elapsed += 10

                # Check if meeting ended or bot was removed
                try:
                    body_text = await page.inner_text("body")
                    if any(phrase in body_text for phrase in [
                        "You've left the call",
                        "The call ended",
                        "removed from the meeting",
                        "Return to home screen",
                        "No one else is here",
                        "denied your request"
                    ]):
                        with STATE_LOCK:
                            BOT_STATE["message"] = "Session ended by teacher/host. Left automatically."
                        break
                except Exception:
                    pass

                # Check live participant counter for auto-leave
                try:
                    badge = page.locator("div.uGOf1d").first
                    if await badge.is_visible():
                        count_str = (await badge.inner_text()).strip()
                        if count_str.isdigit():
                            count = int(count_str)
                            with STATE_LOCK:
                                BOT_STATE["participants"] = count
                                BOT_STATE["message"] = f"In Class • {count} participants present."
                            if count > peak_participants:
                                peak_participants = count
                            if peak_participants >= min_participants and count < min_participants:
                                with STATE_LOCK:
                                    BOT_STATE["message"] = f"Class ended (dropped to {count} students). Auto-left!"
                                break
                except Exception:
                    pass

    except Exception as e:
        with STATE_LOCK:
            BOT_STATE["status"] = "error"
            BOT_STATE["message"] = f"Bot error: {str(e)[:120]}"
    finally:
        try:
            if context:
                await context.close()
            if browser:
                await browser.close()
        except Exception:
            pass
        gc.collect()  # Force free RAM immediately on 512MB container
        with STATE_LOCK:
            if BOT_STATE["status"] != "error":
                BOT_STATE["status"] = "idle"
            BOT_STATE["participants"] = 0
            BOT_STATE["stop_requested"] = False

def run_async_bot(meet_url, student_name, max_minutes, min_participants):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(meet_bot_worker(meet_url, student_name, max_minutes, min_participants))
    loop.close()

@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "GhostAttend Low-RAM Stealth Server", "state": BOT_STATE})

@app.route("/status", methods=["GET"])
def status():
    with STATE_LOCK:
        return jsonify(BOT_STATE)

@app.route("/join", methods=["POST", "GET"])
def join_meeting():
    data = request.get_json(silent=True) or request.args
    meet_url = (data.get("meet_url") or data.get("url") or "").strip()
    student_name = (data.get("student_name") or data.get("name") or "Hrishikesh Dutta").strip()
    max_minutes = int(data.get("max_duration") or data.get("duration") or 50)
    min_participants = int(data.get("min_participants") or 5)

    if not meet_url or "meet.google.com" not in meet_url:
        return jsonify({"ok": False, "error": "Please provide a valid Google Meet link."}), 400

    with STATE_LOCK:
        if BOT_STATE["status"] in ["starting", "in_class"]:
            return jsonify({"ok": False, "error": "A bot is already active in a class. Tap Leave first."}), 409
        BOT_STATE["meet_url"] = meet_url
        BOT_STATE["student_name"] = student_name

    thread = threading.Thread(
        target=run_async_bot,
        args=(meet_url, student_name, max_minutes, min_participants),
        daemon=True
    )
    thread.start()
    return jsonify({"ok": True, "message": "Stealth bot deployed!", "state": BOT_STATE})

@app.route("/leave", methods=["POST", "GET"])
def leave_meeting():
    with STATE_LOCK:
        if BOT_STATE["status"] in ["starting", "in_class"]:
            BOT_STATE["stop_requested"] = True
            BOT_STATE["status"] = "leaving"
            BOT_STATE["message"] = "Disconnecting bot from class..."
            return jsonify({"ok": True, "message": "Kill signal sent. Leaving class now."})
        return jsonify({"ok": True, "message": "No active bot is currently in class."})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    # Single worker + threaded mode prevents Gunicorn/Flask from multiplying RAM usage
    app.run(host="0.0.0.0", port=port, threaded=True)
