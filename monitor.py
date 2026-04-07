from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from bs4 import BeautifulSoup
import time
import os
import requests
import re
import json
import random
import hashlib
from pathlib import Path

# Load environment variables from .env file (local dev)


# ----------------------------
# Persistent state (Option A)
# ----------------------------
STATE_PATH = Path("state/seen.json")

def load_seen_jobs() -> set[str]:
    """Load seen job keys from state/seen.json (committed back by GitHub Actions)."""
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(str(x) for x in data)
        return set()
    except Exception:
        return set()

def save_seen_jobs(seen: set[str]) -> None:
    """Save seen job keys to state/seen.json and log status."""

    try:
        # Ensure directory exists
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        print(f"[STATE] Ensured directory: {STATE_PATH.parent.resolve()}")

        # Write file
        STATE_PATH.write_text(
            json.dumps(sorted(seen), indent=2),
            encoding="utf-8"
        )

        # Confirm file exists + size
        if STATE_PATH.exists():
            size = STATE_PATH.stat().st_size
            print(f"[STATE] Saved file: {STATE_PATH.resolve()}")
            print(f"[STATE] File size: {size} bytes")
            print(f"[STATE] Total jobs saved: {len(seen)}")
        else:
            print("[STATE] ERROR: File was not created")

    except Exception as e:
        print(f"[STATE] ERROR saving state: {e}")


def stable_job_key(job_id: str | None, link: str | None) -> str | None:
    """
    Prefer LinkedIn job_id. If missing, fall back to a stable hash of the link.
    """
    if job_id:
        return f"job:{job_id}"
    if link:
        h = hashlib.sha256(link.encode("utf-8")).hexdigest()[:24]
        return f"linkhash:{h}"
    return None

def small_jitter_sleep(min_s=5, max_s=45):
    """Avoid hitting the site at exact clock boundaries."""
    time.sleep(random.randint(min_s, max_s))


# ----------------------------
# Job classification config
# ----------------------------
JOB_GROUPS = {
    "ML/AI": ["Machine Learning", "AI", "Computer Vision", "LLM", "NLP", "Applied Scientist"],
    "Data/Infra": ["Data Scientist", "Data Engineer", "MLOps", "Database"],
    "Software/Networks": ["Software Engineer", "iOS", "SwiftUI", "Network Engineer", "Security Analyst"],
    "Emerging": ["AI Agent", "Robotics", "Automation"],
}

INTERN_KEYWORDS = ["intern", "internship", "co-op"]

EXCLUDE_KEYWORDS = [
    "senior", "staff", "principal", "lead", "firmware",
    "embedded", "hardware", "kernel", "fpga",
]


def classify_title(title: str) -> str | None:
    """
    Return the job group label if the title matches a group keyword,
    or None if it doesn't match any group.
    Matching is case-insensitive.
    """
    title_lower = title.lower()
    for group, keywords in JOB_GROUPS.items():
        for kw in keywords:
            if kw.lower() in title_lower:
                return group
    return None

# Pull persisted seen jobs (global in-memory set)
seenJobs = load_seen_jobs()

def scrape_jobs():
    # jitter so schedule doesn't look perfectly robotic
    small_jitter_sleep()

    url = "https://www.linkedin.com/jobs/search?keywords=Intern&location=United+States&geoId=103644278&f_TPR=r86400"

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")  # better for newer chrome
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    chrome_bin = os.getenv("CHROME_BIN")
    chromedriver_bin = os.getenv("CHROMEDRIVER_BIN")

    if chrome_bin:
        chrome_options.binary_location = chrome_bin

    service = Service(executable_path=chromedriver_bin) if chromedriver_bin else Service()
    driver = webdriver.Chrome(service=service, options=chrome_options)

    new_jobs_count = 0

    try:
        driver.get(url)

        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        time.sleep(3)

        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")

        for card in soup.select("div.job-search-card"):
            # -----------------------
            # Job ID (PRIMARY KEY)
            # -----------------------
            urn = card.get("data-entity-urn")
            job_id = None
            if urn:
                match = re.search(r"jobPosting:(\d+)", urn)
                if match:
                    job_id = match.group(1)

            # -----------------------
            # Title / Company
            # -----------------------
            title_el = card.select_one("h3.base-search-card__title")
            title = title_el.get_text(strip=True) if title_el else None

            company_el = card.select_one("h4.base-search-card__subtitle a.hidden-nested-link")
            company = company_el.get_text(strip=True) if company_el else None

            # -----------------------
            # Date / time (text + datetime attr)
            # -----------------------
            time_el = card.select_one(
                "time.job-search-card__listdate--new, time.job-search-card__listdate"
            )
            posted_text = time_el.get_text(strip=True) if time_el else None

            # -----------------------
            # Job link
            # -----------------------
            link_el = card.select_one("a.base-card__full-link")
            link = link_el.get("href") if link_el else None

            # -----------------------
            # Build stable key + dedupe
            # -----------------------
            key = stable_job_key(job_id, link)
            if not key:
                continue

            if key in seenJobs:
                continue

            title_lower = title.lower() if title else ""

            # Must contain an intern keyword
            if not any(kw in title_lower for kw in INTERN_KEYWORDS):
                seenJobs.add(key)
                continue

            # Exclude senior / non-student roles
            if any(kw in title_lower for kw in EXCLUDE_KEYWORDS):
                seenJobs.add(key)
                continue

            # Must match a high-level job group
            category = classify_title(title) if title else None
            if not category:
                seenJobs.add(key)
                continue

            # Mark as seen BEFORE sending (idempotency)
            seenJobs.add(key)

            # Send alert with category tag
            send_text_message(company, title, link, posted_text, category)
            new_jobs_count += 1

    except Exception as e:
        send_telegram_message(f"⚠️ Bot error during scraping:\n{e}")
        raise

    finally:
        driver.quit()
        # persist seen jobs for next run
        save_seen_jobs(seenJobs)

    # Send summary to Telegram after scraping completes
    if new_jobs_count > 0:
        send_telegram_message(f"✅ Done! Found {new_jobs_count} new intern posting(s) this run.")
    else:
        send_telegram_message("🔍 Done scraping. No new intern postings found this run.")

def send_telegram_message(text: str) -> None:
    """Send a plain text message to the configured Telegram chat."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("CHAT_ID")

    if not token or not chat_id:
        print("send_telegram_message skipped: missing TELEGRAM_TOKEN or CHAT_ID")
        return

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        params = {"chat_id": chat_id, "text": text}
        response = requests.post(url, params=params, timeout=10)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            print(f"Telegram API error: {result.get('description', 'Unknown error')}")
    except Exception as e:
        print(f"Error sending Telegram message: {e}")


def send_text_message(company, position, link, datetime_text, category="Match"):
    try:
        company = company or "(unknown company)"
        position = position or "(unknown title)"
        link = link or "(no link)"
        datetime_text = datetime_text or "(unknown time)"

        tag = f"[{category} Intern Match]"
        textMessage = (
            f"<b>{tag}</b>\n"
            f"{position} @ {company}\n"
            f"{link}\n"
            f"Posted: {datetime_text}"
        )

        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("CHAT_ID")

        if not token:
            print("Error: TELEGRAM_TOKEN environment variable is not set")
            return {"ok": False, "error": "Missing TELEGRAM_TOKEN"}

        if not chat_id:
            print("Error: CHAT_ID environment variable is not set")
            return {"ok": False, "error": "Missing CHAT_ID"}

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        params = {
            "chat_id": chat_id,
            "text": textMessage,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

        response = requests.post(url, params=params, timeout=10)
        response.raise_for_status()

        result = response.json()

        if not result.get("ok", False):
            error_msg = result.get("description", "Unknown Telegram API error")
            print(f"Error: Telegram API returned error: {error_msg}")
            return result

        print(f"Successfully sent message for {position} at {company}")
        return result

    except requests.exceptions.Timeout:
        print("Error: Request to Telegram API timed out")
        return {"ok": False, "error": "Request timeout"}

    except requests.exceptions.ConnectionError:
        print("Error: Could not connect to Telegram API")
        return {"ok": False, "error": "Connection error"}

    except requests.exceptions.HTTPError as e:
        print(f"Error: HTTP error occurred: {e}")
        return {"ok": False, "error": f"HTTP error: {str(e)}"}

    except requests.exceptions.RequestException as e:
        print(f"Error: Request failed: {e}")
        return {"ok": False, "error": f"Request exception: {str(e)}"}

    except ValueError as e:
        print(f"Error: Invalid JSON response: {e}")
        return {"ok": False, "error": "Invalid JSON response"}

    except Exception as e:
        print(f"Error: Unexpected error in send_text_message: {e}")
        return {"ok": False, "error": f"Unexpected error: {str(e)}"}

def send_startup_notification():
    """Send a Telegram message that the bot is starting a new scrape."""
    send_telegram_message("✅ Connected! Job Monitor bot is running. Scanning for new intern postings...")


if __name__ == "__main__":
    # Verify env vars are present before doing anything
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("CHAT_ID")
    print(f"[ENV] TELEGRAM_TOKEN set: {bool(token)}")
    print(f"[ENV] CHAT_ID set: {bool(chat_id)}")

    send_startup_notification()
    scrape_jobs()
