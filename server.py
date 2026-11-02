import os
import gc
import json
import asyncio
import threading
import subprocess
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

app = Flask(__name__)
CORS(app)

BOT_STATE = {
    "status": "idle",       # idle | starting | in_class | leaving | error
    "message": "Ready to deploy.",
    "participants": 0,
    "meet_url": "",
    "student_name": "",
    "auth_mode": "guest",
    "stop_requested": False
}

LAST_SCREENSHOT = None
STATE_LOCK = threading.Lock()

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
    clean = raw_name.strip()
    if len(clean) > 2:
        return clean[:2] + "\u200c" + clean[2:]
    return clean

def parse_cookies_input(raw_json_str: str):
    if not raw_json_str or not raw_json_str.strip():
        return None, None

    data = json.loads(raw_json_str.strip())
    if isinstance(data, dict) and "cookies" in data:
        with open("auth.json", "w", encoding="utf-8") as f:
            json.dump(data, f)
        return "storage_state", "auth.json"
    elif isinstance(data, list):
        pw_cookies = []
        for c in data:
            if "name" in c and "value" in c and "domain" in c:
                samesite_raw = str(c.get("sameSite", "")).lower()
                if samesite_raw in ["no_restriction", "none"]:
                    samesite_val = "None"
                elif samesite_raw == "strict":
                    samesite_val = "Strict"
                else:
                    samesite_val = "Lax"

                pw_cookies.append({
                    "name": str(c["name"]),
                    "value": str(c["value"]),
                    "domain": str(c["domain"]),
                    "path": str(c.get("path", "/")),
                    "secure": bool(c.get("secure", True)),
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "sameSite": samesite_val
                })
        return "cookie_list", pw_cookies
    return None, None

async def capture_snap(page):
    global LAST_SCREENSHOT
    try:
        LAST_SCREENSHOT = await page.screenshot(type="jpeg", quality=55)
    except Exception:
        pass

async def meet_bot_worker(meet_url: str, raw_name: str, max_minutes: int, min_participants: int, custom_cookies_str: str):
    global LAST_SCREENSHOT
    with STATE_LOCK:
        BOT_STATE["status"] = "starting"
        BOT_STATE["message"] = "Booting stealth browser..."
        BOT_STATE["participants"] = 0
        BOT_STATE["stop_requested"] = False

    safe_name = make_anti_spoof_name(raw_name)
    auth_env = custom_cookies_str.strip() or os.environ.get("GOOGLE_AUTH_STATE", "").strip()
    auth_type, auth_payload = None, None

    if auth_env:
        try:
            auth_type, auth_payload = parse_cookies_input(auth_env)
            with STATE_LOCK:
                BOT_STATE["auth_mode"] = "google_authenticated" if auth_type else "guest"
        except Exception as e:
            print(f"[!] Cookie parse warning: {e}")

    browser = None
    context = None

    try:
        async with async_playwright() as p:
            launch_args = [
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
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=192",
                "--window-size=1280,720"
            ]

            try:
                browser = await p.chromium.launch(
                    headless=True,
                    ignore_default_args=["--enable-automation"],
                    args=launch_args
                )
            except Exception as launch_err:
                if "Executable doesn't exist" in str(launch_err):
                    with STATE_LOCK:
                        BOT_STATE["message"] = "Installing Chromium (one-time ~40s setup)..."
                    subprocess.run(["playwright", "install", "chromium"], check=True)
                    browser = await p.chromium.launch(
                        headless=True,
                        ignore_default_args=["--enable-automation"],
                        args=launch_args
                    )
                else:
                    raise launch_err

            context_kwargs = {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "viewport": {"width": 1280, "height": 720},
                "locale": "en-IN",
                "timezone_id": "Asia/Kolkata",
                "permissions": ["microphone", "camera"]
            }

            if auth_type == "storage_state":
                context_kwargs["storage_state"] = auth_payload

            context = await browser.new_context(**context_kwargs)
            if auth_type == "cookie_list" and auth_payload:
                await context.add_cookies(auth_payload)

            await context.add_init_script(STEALTH_PATCH_JS)
            page = await context.new_page()
            await stealth_async(page)

            async def block_heavy_assets(route):
                if route.request.resource_type in ["font", "media"]:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", block_heavy_assets)

            with STATE_LOCK:
                mode_label = "Authenticated Account" if auth_type else "Stealth Guest"
                BOT_STATE["message"] = f"Loading Google Meet ({mode_label})..."

            await page.goto(meet_url, wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(8)
            await capture_snap(page)

            # Hardware mute Mic (Ctrl+D) and Cam (Ctrl+E)
            await page.keyboard.press("Control+d")
            await asyncio.sleep(1)
            await page.keyboard.press("Control+e")

            # Smart Lobby Handler: Loops for up to 35 seconds on slow 0.1 CPU to clear popups, fill name, and click Join
            joined_clicked = False
            name_typed = False

            for attempt in range(12):
                with STATE_LOCK:
                    if BOT_STATE["stop_requested"]:
                        break

                await capture_snap(page)

                # Check if Google blocked anonymous access completely
                body_txt = ""
                try:
                    body_txt = await page.inner_text("body")
                except Exception:
                    pass

                if "You can't join this call" in body_txt or "Sign in to join" in body_txt:
                    with STATE_LOCK:
                        BOT_STATE["status"] = "error"
                        BOT_STATE["message"] = "Host blocked guests! Paste your Google Cookies in the app first."
                    return

                # 1. Dismiss permission/warning popups
                for popup_text in [
                    "Continue without microphone and camera",
                    "Continue without microphone",
                    "Allow microphone and camera",
                    "Got it",
                    "Dismiss"
                ]:
                    try:
                        p_btn = page.locator(f"button:has-text('{popup_text}'), [role='button']:has-text('{popup_text}')").first
                        if await p_btn.is_visible():
                            await p_btn.click(force=True)
                            await asyncio.sleep(1)
                    except Exception:
                        pass

                # 2. Fill Custom Name if guest name box is visible
                if not name_typed:
                    try:
                        name_input = page.locator("input[type='text'], input[placeholder*='name' i], input[aria-label*='name' i]").first
                        if await name_input.is_visible():
                            with STATE_LOCK:
                                BOT_STATE["message"] = f"Typing name: {raw_name}..."
                            await name_input.click(force=True)
                            await asyncio.sleep(0.4)
                            await name_input.fill("")
                            await name_input.type(safe_name, delay=75)
                            name_typed = True
                            await asyncio.sleep(1)
                    except Exception:
                        pass

                # 3. Search for Join / Ask to join button via Playwright + JS fallback
                for btn_label in ["Ask to join", "Join now", "Switch here", "Join"]:
                    try:
                        btn = page.locator(
                            f"button:has-text('{btn_label}'), [role='button']:has-text('{btn_label}'), span:has-text('{btn_label}')"
                        ).first
                        if await btn.is_visible():
                            await btn.click(force=True)
                            joined_clicked = True
                            break
                    except Exception:
                        pass

                if joined_clicked:
                    break

                await asyncio.sleep(3)

            await capture_snap(page)

            if not joined_clicked:
                with STATE_LOCK:
                    BOT_STATE["status"] = "error"
                    BOT_STATE["message"] = "Join button not found! Tap '📸 View Live Snapshot' to see what Google showed."
                return

            with STATE_LOCK:
                BOT_STATE["status"] = "in_class"
                BOT_STATE["message"] = "Active in Class / Waiting Room (Mic & Cam OFF)."

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
                await capture_snap(page)

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
                            BOT_STATE["message"] = "Session ended by teacher. Left automatically."
                        break
                except Exception:
                    pass

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
            BOT_STATE["message"] = f"Bot error: {str(e)[:140]}"
    finally:
        try:
            if context:
                await context.close()
            if browser:
                await browser.close()
        except Exception:
            pass
        gc.collect()
        with STATE_LOCK:
            if BOT_STATE["status"] != "error":
                BOT_STATE["status"] = "idle"
            BOT_STATE["participants"] = 0
            BOT_STATE["stop_requested"] = False

def run_async_bot(meet_url, student_name, max_minutes, min_participants, custom_cookies):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(meet_bot_worker(meet_url, student_name, max_minutes, min_participants, custom_cookies))
    loop.close()

@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "GhostAttend Low-RAM Stealth Server", "state": BOT_STATE})

@app.route("/status", methods=["GET"])
def status():
    with STATE_LOCK:
        return jsonify(BOT_STATE)

@app.route("/view", methods=["GET"])
def view_snapshot():
    """Live snapshot endpoint ending with /view so you can see what the bot sees."""
    if LAST_SCREENSHOT:
        return Response(LAST_SCREENSHOT, mimetype="image/jpeg")
    return "<h3>No snapshot captured yet. Deploy the bot first!</h3>", 404

@app.route("/join", methods=["POST", "GET"])
def join_meeting():
    data = request.get_json(silent=True) or request.args
    meet_url = (data.get("meet_url") or data.get("url") or "").strip()
    student_name = (data.get("student_name") or data.get("name") or "Hrishikesh Dutta").strip()
    max_minutes = int(data.get("max_duration") or data.get("duration") or 125)
    min_participants = int(data.get("min_participants") or 5)
    custom_cookies = (data.get("google_cookies") or "").strip()

    if not meet_url or "meet.google.com" not in meet_url:
        return jsonify({"ok": False, "error": "Please provide a valid Google Meet link."}), 400

    with STATE_LOCK:
        if BOT_STATE["status"] in ["starting", "in_class"]:
            return jsonify({"ok": False, "error": "A bot is already active. Tap Leave first."}), 409
        BOT_STATE["meet_url"] = meet_url
        BOT_STATE["student_name"] = student_name

    thread = threading.Thread(
        target=run_async_bot,
        args=(meet_url, student_name, max_minutes, min_participants, custom_cookies),
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
    app.run(host="0.0.0.0", port=port, threaded=True)
