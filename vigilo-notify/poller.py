#!/usr/bin/env python3
"""
Vigilo message poller.

Reads credentials from environment variables, polls for new message threads,
and forwards them via SMTP. Tracks seen thread IDs in a statefile to avoid
duplicate emails.

Environment variables required:
    VIGILO_ACCESS_TOKEN   - current OAuth access token
    VIGILO_REFRESH_TOKEN  - OAuth refresh token (persisted back to statefile)
    VIGILO_USER_ID        - Vigilo user ID (from bootstrap)
    SMTP_HOST             - SMTP relay host (default: smtp-relay.automation.svc.cluster.local)
    SMTP_PORT             - SMTP relay port (default: 25)
    SMTP_FROM             - sender address (default: post@brujordet.no)
    SMTP_TO               - recipient address
    STATE_FILE            - path to seen-IDs statefile (default: /data/seen.json)
    TOKEN_FILE            - path to persist refreshed tokens (default: /data/tokens.json)
"""

import json
import os
import smtplib
import sys
import base64
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import httpx

# Extracted from the Android APK (public app, not personal credentials).
# Injected via VIGILO_CLIENT_ID / VIGILO_CLIENT_SECRET environment variables.
CLIENT_ID = os.environ.get("VIGILO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("VIGILO_CLIENT_SECRET", "")
AUTH_BASE = "https://auth.prod.vigilo-oas.no"
API_BASE = "https://api-gw-parent-app.prod.vigilo-oas.no"
APP_VERSION = "Android 3.1.4-15"

_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()


def refresh_token(refresh_tok: str) -> dict:
    r = httpx.post(
        f"{AUTH_BASE}/connect/token",
        headers={"Authorization": f"Basic {_basic}"},
        data={"refresh_token": refresh_tok, "grant_type": "refresh_token"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def load_tokens(token_file: Path) -> dict:
    """Load tokens from file, falling back to env vars."""
    if token_file.exists():
        return json.loads(token_file.read_text())
    return {
        "access_token": os.environ["VIGILO_ACCESS_TOKEN"],
        "refresh_token": os.environ["VIGILO_REFRESH_TOKEN"],
    }


def save_tokens(token_file: Path, tokens: dict) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps(tokens))


def load_seen(state_file: Path) -> set:
    if state_file.exists():
        return set(json.loads(state_file.read_text()))
    return set()


def save_seen(state_file: Path, seen: set) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(list(seen)))


def send_email(smtp_host: str, smtp_port: int, from_addr: str, to_addr: str,
               subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
        s.sendmail(from_addr, [to_addr], msg.as_string())


class VigiloClient:
    def __init__(self, access_token: str, user_id: str) -> None:
        self._http = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {access_token}",
                "appVersion": APP_VERSION,
            },
            timeout=30,
        )
        self._user_id = user_id

    def get_children(self) -> list:
        r = self._http.get("/api/children", params={"userId": self._user_id})
        r.raise_for_status()
        data = r.json()
        return data.get("items", data) if isinstance(data, dict) else data

    def get_message_threads(self, child_ids: list, page_size: int = 50) -> dict:
        r = self._http.get(
            "/api/message-threads",
            params={
                "userId": self._user_id,
                "childIds": child_ids,
                "pageSize": page_size,
                "unreadMessageThreadsCountOnly": "false",
                "includeAfterSchoolMessages": "false",
            },
        )
        r.raise_for_status()
        return r.json()

    def get_thread(self, thread_uid: str) -> dict:
        r = self._http.get(
            f"/api/messages/threads/{thread_uid}",
            params={"userId": self._user_id, "pageSize": 50},
        )
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._http.close()


def format_email_body(thread: dict, messages: list) -> str:
    lines = []
    title = thread.get("title", "(no title)")
    sender = thread.get("sender", {})
    sender_name = sender.get("name", "Unknown") if isinstance(sender, dict) else str(sender)
    org = thread.get("organizationalUnit", {})
    org_name = org.get("name", "") if isinstance(org, dict) else str(org)

    lines.append(f"School: {org_name}" if org_name else "")
    lines.append(f"From: {sender_name}")
    lines.append(f"Subject: {title}")
    lines.append("")

    for msg in messages:
        body = msg.get("body", "")
        created = msg.get("timeCreated", "")
        msg_sender = msg.get("sender", {})
        msg_sender_name = msg_sender.get("name", "") if isinstance(msg_sender, dict) else ""
        lines.append(f"--- {msg_sender_name} ({created}) ---")
        lines.append(body)
        lines.append("")

    return "\n".join(line for line in lines if line is not None)


def main() -> None:
    smtp_host = os.environ.get("SMTP_HOST", "smtp-relay.automation.svc.cluster.local")
    smtp_port = int(os.environ.get("SMTP_PORT", "25"))
    smtp_from = os.environ.get("SMTP_FROM", "post@brujordet.no")
    smtp_to = os.environ.get("SMTP_TO", "")
    state_file = Path(os.environ.get("STATE_FILE", "/data/seen.json"))
    token_file = Path(os.environ.get("TOKEN_FILE", "/data/tokens.json"))
    user_id = os.environ.get("VIGILO_USER_ID", "")

    if not smtp_to:
        print("ERROR: SMTP_TO not set", file=sys.stderr)
        sys.exit(1)
    if not user_id:
        print("ERROR: VIGILO_USER_ID not set", file=sys.stderr)
        sys.exit(1)

    # Load and potentially refresh tokens
    tokens = load_tokens(token_file)
    try:
        new_tokens = refresh_token(tokens["refresh_token"])
        tokens.update(new_tokens)
        save_tokens(token_file, tokens)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 401):
            # Refresh token expired — alert the user
            subject = "[Vigilo] Re-authentication required"
            body = (
                "The Vigilo refresh token has expired.\n\n"
                "Please re-run the bootstrap script on your local machine:\n\n"
                "  cd ~/src/gitops-homelab/services/vigilo-notify\n"
                "  python bootstrap.py\n\n"
                "This will open a browser for you to log in again."
            )
            try:
                send_email(smtp_host, smtp_port, smtp_from, smtp_to, subject, body)
                print("Refresh token expired — alert email sent.")
            except Exception as mail_err:
                print(f"Refresh token expired and could not send alert: {mail_err}", file=sys.stderr)
            sys.exit(0)
        raise

    client = VigiloClient(tokens["access_token"], user_id)
    seen = load_seen(state_file)
    new_seen = set(seen)
    emails_sent = 0

    try:
        children = client.get_children()
        child_ids = [str(c.get("id") or c.get("personId", "")) for c in children]

        if not child_ids:
            print("No children found for this account.")
            sys.exit(0)

        threads_data = client.get_message_threads(child_ids)
        threads = threads_data.get("messageThreads", [])

        for thread in threads:
            thread_uid = thread.get("threadUid", "")
            if not thread_uid or thread_uid in seen:
                continue

            # Fetch full thread to get message bodies
            try:
                detail = client.get_thread(thread_uid)
            except Exception as e:
                print(f"Failed to fetch thread {thread_uid}: {e}", file=sys.stderr)
                continue

            messages = detail.get("messages", [])
            title = thread.get("title", "(no title)")
            subject = f"[Vigilo] {title}"
            body = format_email_body(thread, messages)

            try:
                send_email(smtp_host, smtp_port, smtp_from, smtp_to, subject, body)
                new_seen.add(thread_uid)
                emails_sent += 1
                print(f"Emailed thread: {title}")
            except Exception as e:
                print(f"Failed to send email for thread {thread_uid}: {e}", file=sys.stderr)

    finally:
        client.close()

    save_seen(state_file, new_seen)
    print(f"Done. {emails_sent} new message(s) forwarded.")


if __name__ == "__main__":
    main()
