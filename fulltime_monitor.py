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


# ----------------------------
# Persistent state
# ----------------------------
STATE_PATH = Path("state/seen_fulltime.json")


def load_seen_jobs() -> set[str]:
    """Load seen job keys from state/seen_fulltime.json."""
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
    """Save seen job keys to state/seen_fulltime.json and log status."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        print(f"[STATE] Ensured directory: {STATE_PATH.parent.resolve()}")

        STATE_PATH.write_text(
            json.dumps(sorted(seen), indent=2),
            encoding="utf-8"
        )

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
    "Software": ["Software Engineer", "Full Stack", "iOS", "SwiftUI", "App Developer", "Python Developer"],
    "Data/Infra": ["Data Scientist", "Data Engineer", "MLOps", "Database"],
    "Emerging/Networks": ["AI Agent", "Robotics", "Automation", "Network Engineer", "Security Analyst"],
}

# Titles must contain at least one of these to be considered full-time / entry-level
ENTRY_LEVEL_KEYWORDS = ["new grad", "entry level", "university graduate", "class of 2026", "early career"]

# Titles containing any of these are excluded
EXCLUDE_KEYWORDS = [
    "intern", "internship", "co-op",
    "junior", "associate", "experienced",
    "senior", "staff", "principal", "lead",
    "firmware", "embedded", "hardware", "kernel", "circuit", "fpga",
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


def is_entry_level(title: str) -> bool:
    """Return True if the title contains at least one seniority include keyword."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in ENTRY_LEVEL_KEYWORDS)


def is_excluded(title: str) -> bool:
    """Return True if the title contains any exclude keyword."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in EXCLUDE_KEYWORDS)


# Pull persisted seen jobs (global in-memory set)
seenJobs = load_seen_jobs()


def scrape_jobs():
    # Jitter so schedule doesn't look perfectly robotic
    small_jitter_sleep()

    # Search for entry-level / new grad full-time roles posted in the last 24 h
    url = (
        "https://www.linkedin.com/jobs/search"
        "?keywords=New+Grad+OR+University+Graduate+OR+Entry+Level"
        "&location=United+States"
        "&geoId=103644278"
        "&f_TPR=r86400"
    )

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
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

            # Exclude any title with disqualifying keywords (intern, senior, hardware, etc.)
            if is_excluded(title_lower):
                seenJobs.add(key)
                continue

            # Must match a high-level job group
            category = classify_title(title) if title else None
            if not category:
                seenJobs.add(key)
                continue

            # Must contain an entry-level / new-grad seniority signal
            if not is_entry_level(title_lower):
                seenJobs.add(key)
                continue

            # Mark as seen BEFORE sending (idempotency)
            seenJobs.add(key)

            send_text_message(company, title, link, posted_text, category)
            new_jobs_count += 1

    except Exception as e:
        send_telegram_message(f"⚠️ Full-Time Bot error during scraping:\n{e}")
        raise

    finally:
        driver.quit()
        save_seen_jobs(seenJobs)

    if new_jobs_count > 0:
        send_telegram_message(f"✅ Done! Found {new_jobs_count} new full-time posting(s) this run.")
    else:
        send_telegram_message("🔍 Done scraping. No new full-time postings found this run.")


def send_telegram_message(text: str) -> None:
    """Send a plain text message using the full-time Telegram credentials."""
    token = os.getenv("FULLTIME_TELEGRAM_TOKEN")
    chat_id = os.getenv("FULLTIME_CHAT_ID")

    if not token or not chat_id:
        print("send_telegram_message skipped: missing FULLTIME_TELEGRAM_TOKEN or FULLTIME_CHAT_ID")
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
    """Send a formatted full-time job alert via Telegram."""
    try:
        company = company or "(unknown company)"
        position = position or "(unknown title)"
        link = link or "(no link)"
        datetime_text = datetime_text or "(unknown time)"

        tag = f"[Full-Time Match - {category}]"
        textMessage = (
            f"<b>{tag}</b>\n"
            f"{position} @ {company}\n"
            f"{link}\n"
            f"Posted: {datetime_text}"
        )

        token = os.getenv("FULLTIME_TELEGRAM_TOKEN")
        chat_id = os.getenv("FULLTIME_CHAT_ID")

        if not token:
            print("Error: FULLTIME_TELEGRAM_TOKEN environment variable is not set")
            return {"ok": False, "error": "Missing FULLTIME_TELEGRAM_TOKEN"}

        if not chat_id:
            print("Error: FULLTIME_CHAT_ID environment variable is not set")
            return {"ok": False, "error": "Missing FULLTIME_CHAT_ID"}

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
    """Send a Telegram message that the full-time bot is starting a new scrape."""
    send_telegram_message(
        "✅ Connected! Full-Time Job Monitor bot is running. "
        "Scanning for new entry-level / new-grad postings..."
    )


if __name__ == "__main__":
    # Verify env vars are present before doing anything
    token = os.getenv("FULLTIME_TELEGRAM_TOKEN")
    chat_id = os.getenv("FULLTIME_CHAT_ID")
    print(f"[ENV] FULLTIME_TELEGRAM_TOKEN set: {bool(token)}")
    print(f"[ENV] FULLTIME_CHAT_ID set: {bool(chat_id)}")

    send_startup_notification()
    scrape_jobs()
