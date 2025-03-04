import os
import json
import asyncio
import aiohttp
from datetime import datetime
from aiohttp import ClientTimeout
from dotenv import load_dotenv  # Import python-dotenv

# Load environment variables from .env file.
load_dotenv()

# --- Configuration and Constants ---

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
LOCK_FILE = os.path.join(os.path.dirname(__file__), "state.lock")
POLL_INTERVAL_MS = 60000  # default poll interval in milliseconds
REQUEST_TIMEOUT = 15  # seconds

# Load configuration from config.json if available.
config = {"accounts": [], "pollInterval": POLL_INTERVAL_MS}
try:
    with open(CONFIG_FILE, "r") as f:
        file_config = json.load(f)
        config.update(file_config)
except Exception as e:
    print("config.json not found or invalid. Falling back to environment variables.")

# Determine accounts to monitor.
accounts = config.get("accounts", [])
if not accounts and os.environ.get("GITHUB_USER"):
    accounts = [os.environ["GITHUB_USER"]]
if not accounts:
    print("No accounts specified to monitor. Set accounts in config.json or GITHUB_USER env variable.")
    exit(1)

# Convert poll interval from ms to seconds.
POLL_INTERVAL = config.get("pollInterval", POLL_INTERVAL_MS) / 1000

# GitHub and Telegram configuration from environment.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    print("Error: GITHUB_TOKEN must be set as an environment variable.")
    exit(1)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_MESSAGE_THREAD_ID = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID")

# --- Helper Functions ---

def safe_json_parse(text):
    try:
        return json.loads(text)
    except Exception as e:
        print("JSON parse error:", e)
        return None

def get_next_page_url(link_header):
    if not link_header:
        return None
    # The Link header is a comma-separated list of links.
    for link in link_header.split(","):
        parts = link.split(";")
        if len(parts) < 2:
            continue
        url_part, rel_part = parts[0].strip(), parts[1].strip()
        if 'rel="next"' in rel_part:
            # Remove angle brackets
            return url_part[1:-1]
    return None

# --- File Locking for State Management ---

async def acquire_lock(max_attempts=10):
    attempts = 0
    while attempts < max_attempts:
        try:
            # Open file in exclusive creation mode.
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return
        except FileExistsError:
            await asyncio.sleep(0.1)
            attempts += 1
    raise Exception("Unable to acquire file lock.")

async def release_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass

async def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"accounts": {}}

async def save_state(state):
    await acquire_lock()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        # Set file permissions to 600.
        os.chmod(STATE_FILE, 0o600)
    finally:
        await release_lock()

# --- HTTP Request with Retries and Exponential Backoff ---

async def api_request(session, url, attempt=1):
    headers = {
        "User-Agent": "GitHub-Monitor/1.0",
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        async with session.get(url, headers=headers, timeout=ClientTimeout(total=REQUEST_TIMEOUT)) as response:
            text = await response.text()
            if response.status == 403:
                data = safe_json_parse(text)
                if data and ("rate limit" in data.get("message", "").lower() or "abuse" in data.get("message", "").lower()):
                    wait_time = (2 ** attempt)
                    print(f"Rate limit/abuse detected. Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    if attempt < 3:
                        return await api_request(session, url, attempt + 1)
                    else:
                        raise Exception("Max retries reached due to rate limits.")
            if 200 <= response.status < 300:
                return safe_json_parse(text), response.headers
            raise Exception(f"Request failed (status {response.status}): {text}")
    except Exception as e:
        if attempt < 3:
            wait_time = (2 ** attempt)
            print(f"Request error: {e}. Retrying in {wait_time} seconds...")
            await asyncio.sleep(wait_time)
            return await api_request(session, url, attempt + 1)
        else:
            raise

async def fetch_events(session, url):
    events = []
    next_url = url
    while next_url:
        try:
            data, headers = await api_request(session, next_url)
            if isinstance(data, list):
                events.extend(data)
            next_url = get_next_page_url(headers.get("Link"))
        except Exception as e:
            print("Error fetching events:", e)
            break
    return events

# --- Telegram Notification ---

async def send_to_telegram(session, message):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    # Include message_thread_id if defined.
    if TELEGRAM_MESSAGE_THREAD_ID:
        payload["message_thread_id"] = TELEGRAM_MESSAGE_THREAD_ID
    try:
        async with session.post(telegram_url, json=payload) as resp:
            resp_data = await resp.json()
            if not resp_data.get("ok"):
                print("Failed to send Telegram message:", resp_data)
    except Exception as e:
        print("Error sending Telegram message:", e)

# --- Event Formatting and Processing ---

async def print_event(event, session):
    created_at = datetime.fromisoformat(event.get("created_at").replace("Z", "+00:00"))
    local_time = created_at.strftime("%Y-%m-%d %H:%M:%S %Z")
    iso_time = created_at.isoformat()

    # Parse repository information.
    repo_full = event.get("repo", {}).get("name", "")
    repo_parts = repo_full.split("/") if repo_full else []
    owner, repo_name = (repo_parts[0], repo_parts[1]) if len(repo_parts) == 2 else (repo_full, None)
    repo_link = f"https://github.com/{owner}/{repo_name}" if repo_name else ""

    emoji = ""
    description = ""

    event_type = event.get("type")
    if event_type == "CreateEvent":
        emoji = "🆕"
        description = "Repository Created"
    elif event_type == "DeleteEvent":
        emoji = "❌"
        description = "Repository Deleted"
    elif event_type == "PushEvent":
        emoji = "🔄"
        description = "Code Updated"
    else:
        emoji = "ℹ️"
        description = event_type

    # Build console message (with ANSI escape codes).
    console_message = (
        f"\n\x1b[1;34m📅 New Activity Detected\x1b[0m (\x1b[1;32m{local_time}\x1b[0m)\n"
        f"\x1b[1;33m{emoji} {description}\x1b[0m\n"
    )
    if repo_full:
        console_message += f"\x1b[1;36m📂 {repo_full}\x1b[0m\n"
    console_message += f"\x1b[1;35m⏰ {iso_time}\x1b[0m\n"

    if event_type == "PushEvent":
        commit_count = len(event.get("payload", {}).get("commits", []))
        console_message += f"\x1b[1;37m📝 {commit_count} new commit{'s' if commit_count != 1 else ''}\x1b[0m\n"
        compare_url = event.get("payload", {}).get("compare")
        if compare_url:
            console_message += f"\x1b[1;32m🔗 Compare Changes: {compare_url}\x1b[0m\n"
    elif event.get("payload", {}).get("action"):
        console_message += f"\x1b[1;37mℹ️ Action: {event['payload']['action']}\x1b[0m\n"

    print(console_message)

    # Build Telegram message (HTML formatted).
    telegram_message = (
        "<b>📅 New Activity Detected</b>\n\n"
        f"<a>{emoji} {description}</a>\n\n"
        f"<a>👤 {owner}</a>\n\n"
    )
    if repo_name:
        telegram_message += f"<b>📦</b> {repo_name}\n\n"
    telegram_message += f"<a>⏰</a> {iso_time}\n\n"
    if event_type == "PushEvent":
        commit_count = len(event.get("payload", {}).get("commits", []))
        telegram_message += f"<a>📝</a> {commit_count} new commit{'s' if commit_count != 1 else ''}\n\n"
        if event.get("payload", {}).get("compare"):
            telegram_message += f'<a>🔗</a> <a href="{event["payload"]["compare"]}">Compare Changes</a>\n\n'
    elif event.get("payload", {}).get("action"):
        telegram_message += f"<a>ℹ️</a> Action: {event['payload']['action']}\n\n"
    if repo_link:
        telegram_message += f'<a href="{repo_link}">🌐 View Repository</a>'
    
    await send_to_telegram(session, telegram_message)
    # Delay between processing events (5 seconds).
    await asyncio.sleep(5)

async def poll_account(session, account, last_check):
    url = f"https://api.github.com/users/{account}/events"
    print(f"\nPolling events for {account}...")
    events = await fetch_events(session, url)

    # Filter events that occurred after last_check.
    if last_check:
        last_check_dt = datetime.fromisoformat(last_check)
        new_events = [e for e in events if datetime.fromisoformat(e.get("created_at").replace("Z", "+00:00")) > last_check_dt]
    else:
        new_events = events

    if not new_events:
        print(f"No new events for {account} since last check.")
    else:
        # Sort events in ascending order.
        new_events.sort(key=lambda e: datetime.fromisoformat(e.get("created_at").replace("Z", "+00:00")))
        for event in new_events:
            await print_event(event, session)
    # Return new lastCheck as current time.
    return datetime.utcnow().isoformat()

async def validate_token(session):
    try:
        data, _ = await api_request(session, "https://api.github.com/user")
        if not data or data.get("message"):
            raise Exception("Invalid GitHub token.")
        print("GitHub token validated successfully.")
    except Exception as e:
        print("GitHub token validation failed:", e)
        exit(1)

# --- Main Polling Loop ---

async def run_poller():
    async with aiohttp.ClientSession() as session:
        await validate_token(session)
        state = await load_state()
        if "accounts" not in state:
            state["accounts"] = {}
        # Initialize state for each account.
        for account in accounts:
            if account not in state["accounts"]:
                state["accounts"][account] = {"lastCheck": None}

        while True:
            print(f"\n--- Polling Cycle at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
            for account in accounts:
                try:
                    last_check = state["accounts"][account].get("lastCheck")
                    new_last_check = await poll_account(session, account, last_check)
                    state["accounts"][account]["lastCheck"] = new_last_check
                except Exception as e:
                    print(f"Error polling {account}:", e)
            await save_state(state)
            print(f"Cycle complete. Waiting {POLL_INTERVAL} seconds for next poll...\n")
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(run_poller())
    except Exception as e:
        print("Fatal error:", e)
        exit(1)
