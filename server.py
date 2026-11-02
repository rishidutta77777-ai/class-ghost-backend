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

if (navigator.mediaDevices) {
  navigator.mediaDevices.getUserMedia = async () => {
    throw new DOMException('Permission denied', 'NotAllowedError');
  };
  navigator.mediaDevices.enumerateDevices = async () => [];
}
"""

def make_anti_spoof_name(raw_name: str) -> str:
    clean = raw_name.strip()
    if len(clean) > 2:
        return clean[:2] + "\u200c" + clean[2:]
    return clean

def build_storage_state(raw_json_str: str):
    """
    Converts Cookie-Editor JSON array or raw storage_state into a 
    Playwright-native auth.json storage state structure.
    """
    if not raw_json_str or not raw_json_str.strip():
        return None

    try:
        data = json.loads(raw_json_str.strip())
        cookies_list = []

        if isinstance(data, dict) and "cookies" in data:
            cookies_list = data["cookies"]
        elif isinstance(data, list):
            cookies_list = data
        else:
            return None

        formatted_cookies = []
        for c in cookies_list:
            if not isinstance(c, dict) or "name" not in c or "value" not in c:
                continue
            name = str(c["name"])
            val = str(c["value"])
            raw_domain = str(c.get("domain", ".google.com")).strip()
            path = str(c.get("path", "/"))

            samesite_raw = str(c.get("sameSite", "")).lower()
            if samesite_raw in ["no_restriction", "none"]:
                samesite_val = "None"
            elif samesite_raw == "strict":
                samesite_val = "Strict"
            else:
                samesite_val = "Lax"

            is_secure = (
                bool(c.get("secure", True))
                or name.startswith("__Secure-")
                or name.startswith("__Host-")
                or samesite_val == "None"
            )

            cookie_obj = {
                "name": name,
                "value": val,
                "domain": raw_domain if raw_domain.startswith(".") else "." + raw_domain,
                "path": path,
                "expires": float(c.get("expirationDate", 1893456000)),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": is_secure,
                "sameSite": samesite_val
            }
            formatted_cookies.append(cookie_obj)

        storage_data = {
            "cookies": formatted_cookies,
            "origins": []
        }

        with open("auth.json", "w", encoding="utf-8") as f:
            json.dump(storage_data, f)
        
        return "auth.json"
    except Exception as e:
        print(f"[!] Storage state build error: {e}")
        return None

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
    storage_file = build_storage_state(auth_env)

    with STATE_LOCK:
        BOT_STATE["auth_mode"] = "google_authenticated" if storage_file else "guest"

    browser = None
    context = None

    try:
        async with async_playwright() as p:
            launch_args = [
                "--headless=new",
                "--disable-blink-features=AutomationControlled",
                "--mute-audio",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--disable-background-networking",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=256",
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
                "permissions": []
            }

            if storage_file and os.path.exists(storage_file):
                context_kwargs["storage_state"] = storage_file

            context = await browser.new_context(**context_kwargs)
            await context.add_init_script(STEALTH_PATCH_JS)
            page = await context.new_page()
            await stealth_async(page)

            with STATE_LOCK:
                mode_label = "Authenticated Profile" if storage_file else "Stealth Guest"
                BOT_STATE["message"] = f"Loading Google Meet ({mode_label})..."

            await page.goto(meet_url, wait_until="domcontentloaded", timeout=90000)
            await asyncio.sleep(5)
            await capture_snap(page)

            # If Google still redirects to sign-in, clear auth file and fall back to guest mode gracefully
            if "accounts.google.com" in page.url:
                with STATE_LOCK:
                    BOT_STATE["message"] = "Session token IP-locked by Google. Falling back to clean Guest Mode..."
                await page.goto(meet_url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)
                await capture_snap(page)

            joined_clicked = False
            name_typed = False

            for attempt in range(20):
                with STATE_LOCK:
                    if BOT_STATE["stop_requested"]:
                        break

                await capture_snap(page)
                body_txt = ""
                try:
                    body_txt = await page.inner_text("body")
                except Exception:
                    pass

                if "Getting ready..." in body_txt:
                    with STATE_LOCK:
                        BOT_STATE["message"] = f"Waiting for Meet lobby ({attempt+1}/20)..."

                if "You can't join this call" in body_txt or "Return to home screen" in body_txt:
                    with STATE_LOCK:
                        BOT_STATE["status"] = "error"
                        BOT_STATE["message"] = "Meeting restricted or closed! Check /view screenshot."
                    return

                # Dismiss popups
                for popup_text in ["Continue without microphone and camera", "Continue without microphone", "Got it", "Dismiss", "Close"]:
                    try:
                        p_btn = page.locator(f"button:has-text('{popup_text}'), [role='button']:has-text('{popup_text}')").first
                        if await p_btn.is_visible():
                            await p_btn.click(force=True)
                            await asyncio.sleep(1)
                    except Exception:
                        pass

                # Type custom name if name box is visible
                if not name_typed:
                    try:
                        name_input = page.locator("input[placeholder*='name' i], input[aria-label*='name' i]").first
                        if await name_input.is_visible():
                            with STATE_LOCK:
                                BOT_STATE["message"] = f"Typing guest name: {raw_name}..."
                            await name_input.click(force=True)
                            await asyncio.sleep(0.3)
                            await name_input.fill(safe_name)
                            name_typed = True
                            await asyncio.sleep(1)
                    except Exception:
                        pass

                # Click Join button
                for btn_label in ["Join now", "Ask to join", "Switch here"]:
                    try:
                        btn = page.locator(f"button:has-text('{btn_label}'), [role='button']:has-text('{btn_label}')").first
                        if await btn.is_visible():
                            await btn.click(force=True)
                            joined_clicked = True
                            break
                    except Exception:
                        pass

                if joined_clicked:
                    break

                await asyncio.sleep(3)

            await asyncio.sleep(3)
            await capture_snap(page)

            if not joined_clicked:
                with STATE_LOCK:
                    BOT_STATE["status"] = "error"
                    BOT_STATE["message"] = "Join button not found! Tap '📸 View Live Screenshot' to check."
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
    with STATE_Loaded := True:
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
