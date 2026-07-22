#!/usr/bin/env python3
"""
gmail_sweeper.py

Fetches unread Gmail messages via the official Gmail API (OAuth 2.0),
summarizes each one with a local Ollama model, and writes the results to
unread_summary.json (plus a human-readable report on the terminal).

Supports multiple Gmail accounts (e.g. personal + academic). Each account
in GMAIL_ACCOUNTS gets its own OAuth client file, matched by position:
the 1st account uses credentials1.json, the 2nd uses credentials2.json,
and so on, each producing its own token file. The gmail.readonly scope
means messages are never modified, so they stay unread regardless of how
many times this script runs.

Bulk/automated mail (marketing, notifications, automated receipts) is
excluded via a heuristic (see _is_bulk_mail) rather than Gmail's
CATEGORY_* labels, since those are inconsistently applied — some accounts
never assign them even to obvious marketing mail. Accounts listed in
GMAIL_NO_BULK_FILTER_ACCOUNTS skip this filter entirely. Separately, for
accounts not listed in GMAIL_UNLIMITED_ACCOUNTS, mail is only in scope if
received after the previous successful sweep of that account -- a rolling
per-account cutoff persisted in .swept_since, so each run only asks Gmail
for what's new since last time instead of re-fetching the same window
repeatedly. The very first sweep of an account falls back to
SWEEP_SINCE_DATE below. GMAIL_UNLIMITED_ACCOUNTS implies both no date
limit and no bulk-mail filter for that account.

A local cache file (processed_ids.json) tracks which message IDs have
already been summarized, so re-running the script only processes messages
that are new since the last run.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
PROCESSED_IDS_PATH = Path(__file__).with_name("processed_ids.json")
SINCE_PATH = Path(__file__).with_name(".swept_since")
OUTPUT_PATH = Path(__file__).with_name("unread_summary.json")

# Only mail received on/after this date is ever swept, forever.
SWEEP_SINCE_DATE = datetime(2026, 7, 19)

MAX_BODY_CHARS = 4000
OLLAMA_TIMEOUT_SECONDS = 120
OLLAMA_STARTUP_TIMEOUT_SECONDS = 30

SIGNATURE_MARKERS = [
    r"^--$",  # standard email signature delimiter ("-- ", trailing space stripped)
    r"^On .+ wrote:$",  # quoted reply header
    r"^_{10,}$",  # Outlook-style separator line
]


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config() -> dict:
    """Load environment variables from .env."""
    load_dotenv()

    labels = [
        label.strip()
        for label in os.getenv("GMAIL_ACCOUNTS", "default").split(",")
        if label.strip()
    ]

    unlimited_labels = {
        label.strip()
        for label in os.getenv("GMAIL_UNLIMITED_ACCOUNTS", "").split(",")
        if label.strip()
    }

    no_bulk_filter_labels = {
        label.strip()
        for label in os.getenv("GMAIL_NO_BULK_FILTER_ACCOUNTS", "").split(",")
        if label.strip()
    } | unlimited_labels

    no_date_limit_labels = unlimited_labels

    return {
        "account_labels": labels,
        "no_date_limit_accounts": no_date_limit_labels,
        "no_bulk_filter_accounts": no_bulk_filter_labels,
        "ollama_base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        "ollama_model": os.getenv("OLLAMA_MODEL", "llama3.1"),
    }


# --------------------------------------------------------------------------
# OAuth 2.0 authentication
# --------------------------------------------------------------------------

def get_credentials_for_account(label: str, credentials_path: Path) -> Credentials:
    """
    Load, refresh, or create OAuth credentials for one Gmail account.
    Each account gets its own OAuth client file (credentials_path) and its
    own token file, so separate accounts stay fully independent.
    """
    token_path = Path(__file__).with_name(f"token_{label}.json")
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                print(
                    f"Error: {credentials_path.name} not found in the project root.\n"
                    "Create an OAuth Client ID (Desktop app) in Google Cloud Console, "
                    "enable the Gmail API, download the client secret, and save it "
                    f"as {credentials_path.name} here.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"Opening browser to authenticate Gmail account '{label}'...")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def get_account_email(service) -> str:
    """Return the email address the given Gmail service is authenticated as."""
    profile = service.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "unknown")


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def load_cache(path: Path) -> set:
    """Return the set of already-processed message IDs, if any."""
    if not path.exists():
        return set()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def save_cache(path: Path, message_ids: set) -> None:
    """Persist the set of processed message IDs as a JSON list."""
    with path.open("w", encoding="utf-8") as f:
        json.dump(sorted(message_ids), f, indent=2)


def load_since_map() -> dict:
    """
    Return {account_label: unix_timestamp}, the end of each account's last
    successful sweep. A label missing from the map hasn't been swept
    before, so it falls back to SWEEP_SINCE_DATE.
    """
    if not SINCE_PATH.exists():
        return {}
    try:
        data = json.loads(SINCE_PATH.read_text())
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_since_map(since_map: dict) -> None:
    """Persist the per-account sweep cutoffs."""
    SINCE_PATH.write_text(json.dumps(since_map, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Gmail API extraction
# --------------------------------------------------------------------------

def _decode_b64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> tuple:
    """Return (body_text, is_html), preferring text/plain anywhere in the MIME tree."""
    html_body = None

    def walk(part):
        nonlocal html_body
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")

        if mime_type == "text/plain" and body_data:
            return _decode_b64url(body_data)
        if mime_type == "text/html" and body_data and html_body is None:
            html_body = _decode_b64url(body_data)

        for sub_part in part.get("parts", []) or []:
            found = walk(sub_part)
            if found is not None:
                return found
        return None

    plain_text = walk(payload)
    if plain_text is not None:
        return plain_text, False
    if html_body is not None:
        return html_body, True
    return "", False


def build_gmail_url(message_id: str) -> str:
    """Build a direct Gmail web link for the given message ID."""
    return "https://mail.google.com/mail/u/0/#search/id%3A" + message_id


_NOISY_CATEGORIES = {
    "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
}
_BULK_SENDER_PATTERN = re.compile(
    r"(no-?reply|do-?not-?reply|notifications?|alerts?|bounce|"
    r"mailer-daemon|postmaster|marketing|newsletter)",
    re.IGNORECASE,
)


def _is_bulk_mail(headers: dict, label_ids: list) -> bool:
    """
    Heuristically identify bulk/automated mail (marketing, notifications,
    automated receipts) to exclude from the Primary-inbox sweep.

    Gmail's CATEGORY_* labels are the ideal signal but are inconsistently
    applied — some accounts (e.g. ones with inbox tabs turned off) never
    assign them even to obvious marketing mail. List-Unsubscribe/List-Id/
    Precedence and common automated-sender address patterns catch what
    category labels miss, including transactional mail like payment
    receipts that never carry an unsubscribe link.
    """
    if _NOISY_CATEGORIES.intersection(label_ids):
        return True
    if "List-Unsubscribe" in headers or "List-Id" in headers:
        return True
    if headers.get("Precedence", "").lower() in ("bulk", "list", "junk"):
        return True
    if _BULK_SENDER_PATTERN.search(headers.get("From", "")):
        return True
    return False


def fetch_unread(service, since_timestamp: int | None, filter_bulk: bool = True) -> list:
    """
    Fetch unread messages for an authenticated Gmail API service. If
    since_timestamp is None, no date cutoff is applied (full unread
    backlog). If filter_bulk is False, bulk/automated mail (see
    _is_bulk_mail) is kept instead of excluded.
    """
    query = "is:unread" if since_timestamp is None else f"is:unread after:{since_timestamp}"
    message_refs = []
    page_token = None
    while True:
        response = (
            service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token)
            .execute()
        )
        message_refs.extend(response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    emails = []
    for ref in message_refs:
        full = (
            service.users()
            .messages()
            .get(userId="me", id=ref["id"], format="full")
            .execute()
        )
        headers = {
            h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])
        }
        if filter_bulk and _is_bulk_mail(headers, full.get("labelIds", [])):
            continue

        body, is_html = _extract_body(full.get("payload", {}))

        emails.append(
            {
                "message_id": full["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", "(no subject)"),
                "date": headers.get("Date", ""),
                "raw_body": body,
                "is_html": is_html,
            }
        )

    return emails


# --------------------------------------------------------------------------
# Body cleaning
# --------------------------------------------------------------------------

class _HTMLTextExtractor(HTMLParser):
    """Minimal stdlib HTML-to-text converter (no external dependency)."""

    _BLOCK_TAGS = {"br", "p", "div", "tr", "li"}
    _SKIP_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__()
        self._chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


def clean_body(raw_body: str, is_html: bool) -> str:
    """Strip HTML/signatures/quoted replies and cap length."""
    if not raw_body:
        return ""

    text = _html_to_text(raw_body) if is_html else raw_body

    # Cut the body at the first signature/quoted-reply marker.
    lines = text.splitlines()
    cut_index = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        for pattern in SIGNATURE_MARKERS:
            if re.match(pattern, stripped):
                cut_index = i
                break
        if cut_index != len(lines):
            break
    text = "\n".join(lines[:cut_index])

    # Collapse excessive blank lines / whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS].rstrip() + "\n[...truncated]"

    return text


# --------------------------------------------------------------------------
# Ollama lifecycle
# --------------------------------------------------------------------------

def _ollama_is_up(base_url: str) -> bool:
    """Return True if something is answering at base_url's /api/tags."""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=3)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def ensure_ollama_running(base_url: str) -> bool:
    """
    Make sure Ollama is reachable at base_url, starting `ollama serve`
    locally if it isn't. Returns True if Ollama ends up reachable.
    """
    if _ollama_is_up(base_url):
        return True

    host = urllib.parse.urlparse(base_url).hostname
    if host not in ("localhost", "127.0.0.1", "::1"):
        print(
            f"Warning: Ollama isn't reachable at {base_url} and it's not a local "
            "host, so it can't be started automatically.",
            file=sys.stderr,
        )
        return False

    if shutil.which("ollama") is None:
        print(
            "Warning: 'ollama' executable not found on PATH; cannot auto-start it.",
            file=sys.stderr,
        )
        return False

    print("Ollama isn't running — starting 'ollama serve'...")
    env = os.environ.copy()
    parsed = urllib.parse.urlparse(base_url)
    if parsed.hostname and parsed.port:
        env["OLLAMA_HOST"] = f"{parsed.hostname}:{parsed.port}"

    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"Warning: failed to start Ollama: {exc}", file=sys.stderr)
        return False

    deadline = time.monotonic() + OLLAMA_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _ollama_is_up(base_url):
            print("Ollama is up.")
            return True
        time.sleep(1)

    print(
        f"Warning: Ollama did not become reachable within "
        f"{OLLAMA_STARTUP_TIMEOUT_SECONDS}s.",
        file=sys.stderr,
    )
    return False


# --------------------------------------------------------------------------
# Ollama summarization
# --------------------------------------------------------------------------

def summarize_email(email_dict: dict, base_url: str, model: str) -> str:
    """Ask the local Ollama model to summarize the email as it sees fit."""
    prompt = (
        "Summarize the following email as bullet points, focusing on the "
        "key takeaways or action items. Use as many or as few bullet points "
        "as the content warrants. Respond with just the bullet points, no "
        "preamble.\n\n"
        f"From: {email_dict['from']}\n"
        f"Subject: {email_dict['subject']}\n"
        f"Body:\n{email_dict['clean_body']}\n"
    )

    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=OLLAMA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
    except requests.RequestException as exc:
        return f"[summary unavailable: {exc}]"
    except (ValueError, KeyError) as exc:
        return f"[summary unavailable: malformed Ollama response ({exc})]"


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

YOUVE_GOT_MAIL_BANNER = r"""
                              _______________
                             |\             /|
                             | \           / |
                             |  \         /  |
                             |   \       /   |
                             |    \     /    |
                             |     \   /     |
                             |      \ /      |
                             |_______V_______|

#   #  ###  #   # #  #   # #####   ####  ###  #####  #   #   #   ### #     #
#   # #   # #   #    #   # #      #     #   #   #    ## ##  # #   #  #     #
 # #  #   # #   #    #   # ####   #  ## #   #   #    # # # #####  #  #     #
  #   #   # #   #     # #  #      #   # #   #   #    #   # #   #  #  #
  #    ###   ###       #   #####   ####  ###    #    #   # #   # ### ##### #
"""


def print_report(results: list) -> None:
    """Print a clean, human-readable summary block to the terminal."""
    print(YOUVE_GOT_MAIL_BANNER)
    print("=" * 70)
    print(f" Gmail Sweeper — {len(results)} new unread email(s) summarized")
    print("=" * 70)

    for r in results:
        print()
        print(f"Account: {r['account']}")
        print(f"Subject: {r['subject']}")
        print(f"From:    {r['from']}")
        print(f"Date:    {r['date']}")
        print(f"Summary:")
        for line in r["summary"].splitlines():
            print(f"  {line}")
        print(f"Link:    {r['gmail_url']}")
        print("-" * 70)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def main() -> None:
    config = load_config()
    processed_ids = load_cache(PROCESSED_IDS_PATH)
    since_map = load_since_map()
    run_start_ts = int(time.time())

    all_emails = []
    swept_labels = []
    for index, label in enumerate(config["account_labels"], start=1):
        credentials_path = Path(__file__).with_name(f"credentials{index}.json")
        creds = get_credentials_for_account(label, credentials_path)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        try:
            account_email = get_account_email(service)
        except HttpError as exc:
            print(f"Warning: could not fetch profile for account '{label}': {exc}", file=sys.stderr)
            account_email = label

        no_date_limit = label in config["no_date_limit_accounts"]
        no_bulk_filter = label in config["no_bulk_filter_accounts"]
        if no_date_limit:
            since_ts = None
            print(f"Sweeping {account_email}: no date limit")
        else:
            since_ts = since_map.get(label, int(SWEEP_SINCE_DATE.timestamp()))
            print(f"Sweeping {account_email}: mail received after {time.ctime(since_ts)}")

        try:
            account_emails = fetch_unread(service, since_ts, filter_bulk=not no_bulk_filter)
        except HttpError as exc:
            print(
                f"Error: Gmail API request failed for account '{label}' ({account_email}): {exc}",
                file=sys.stderr,
            )
            continue

        for e in account_emails:
            e["account"] = account_email
        all_emails.extend(account_emails)
        print(f"Fetched {len(account_emails)} unread email(s) from {account_email}")

        if not no_date_limit:
            swept_labels.append(label)

    new_emails = [e for e in all_emails if e["message_id"] not in processed_ids]

    if not new_emails:
        # Nothing pending, so it's safe to move the cutoff forward now.
        for label in swept_labels:
            since_map[label] = run_start_ts
        save_since_map(since_map)
        print("No new unread emails to summarize (all caught up).")
        return

    ensure_ollama_running(config["ollama_base_url"])

    results = []
    for e in new_emails:
        e["clean_body"] = clean_body(e["raw_body"], e["is_html"])
        summary = summarize_email(e, config["ollama_base_url"], config["ollama_model"])
        gmail_url = build_gmail_url(e["message_id"])

        results.append(
            {
                "account": e["account"],
                "from": e["from"],
                "subject": e["subject"],
                "date": e["date"],
                "message_id": e["message_id"],
                "gmail_url": gmail_url,
                "summary": summary,
            }
        )

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    processed_ids.update(e["message_id"] for e in new_emails)
    save_cache(PROCESSED_IDS_PATH, processed_ids)

    # Everything fetched this run is now either summarized or cached, so the
    # per-account cutoff can safely move forward.
    for label in swept_labels:
        since_map[label] = run_start_ts
    save_since_map(since_map)

    print_report(results)
    print(f"\nSaved {len(results)} summar{'y' if len(results) == 1 else 'ies'} to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
