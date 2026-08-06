import os
import re
import json
import base64
import sqlite3
from datetime import datetime
from email.mime.text import MIMEText
from typing import List, Dict, Optional

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

load_dotenv(override=True)


def _get_secret(key: str, default: str = "") -> str:
    """Read from st.secrets (Streamlit Cloud) first, then fall back to env var."""
    try:
        return st.secrets[key]
    except (KeyError, FileNotFoundError):
        return os.getenv(key, default)


OPENAI_API_KEY = _get_secret("OPENAI_API_KEY")
OPENAI_MODEL = _get_secret("OPENAI_MODEL") or "gpt-4o-mini"
GOOGLE_DRIVE_FOLDER_ID = _get_secret("GOOGLE_DRIVE_FOLDER_ID", "")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
]

DB_FILE = "triage_logs.db"
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

INTENTS = ["Invoice", "Meeting Request", "Spam", "Action Required"]


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gmail_id TEXT,
            sender TEXT,
            subject TEXT,
            intent TEXT,
            action TEXT,
            file_name TEXT,
            reply_sent INTEGER DEFAULT 0,
            drive_uploaded INTEGER DEFAULT 0,
            status TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def log_action(
    gmail_id: str,
    sender: str,
    subject: str,
    intent: str,
    action: str,
    file_name: str = "",
    reply_sent: int = 0,
    drive_uploaded: int = 0,
    status: str = "done",
):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO email_actions
        (gmail_id, sender, subject, intent, action, file_name, reply_sent, drive_uploaded, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (gmail_id, sender, subject, intent, action,
         file_name, reply_sent, drive_uploaded, status),
    )
    conn.commit()
    conn.close()


def get_openai_client():
    if not OPENAI_API_KEY:
        return None
    return OpenAI(api_key=OPENAI_API_KEY)


def _load_credentials_from_secrets() -> Optional[Credentials]:
    """Load Google OAuth token from st.secrets (Streamlit Cloud)."""
    try:
        token_data = st.secrets["google_token"]
        return Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=SCOPES,
        )
    except (KeyError, FileNotFoundError):
        return None


def gmail_auth():
    creds = None

    # Try loading token from Streamlit Cloud secrets first
    creds = _load_credentials_from_secrets()

    # Fallback: load from local token.json (local dev)
    if creds is None and os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Persist refreshed token to local file if possible
            try:
                with open(TOKEN_FILE, "w", encoding="utf-8") as token:
                    token.write(creds.to_json())
            except Exception:
                pass
        else:
            # On Streamlit Cloud: no local browser — show instructions
            is_cloud = not os.path.exists(CREDENTIALS_FILE)
            if is_cloud:
                raise FileNotFoundError(
                    "Google credentials not found in st.secrets. "
                    "Please add [google_token] to your Streamlit Cloud secrets. "
                    "See the README for setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w", encoding="utf-8") as token:
                token.write(creds.to_json())

    gmail_service = build("gmail", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gmail_service, drive_service


def decode_mime_text(value: Optional[str]) -> str:
    if not value:
        return ""
    from email.header import decode_header
    parts = decode_header(value)
    decoded = ""
    for part, encoding in parts:
        if isinstance(part, bytes):
            decoded += part.decode(encoding or "utf-8", errors="ignore")
        else:
            decoded += part
    return decoded


def extract_email_body(payload: Dict) -> str:
    parts = payload.get("parts", [])
    if not parts:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data.encode("UTF-8")).decode("utf-8", errors="ignore")
        return ""

    for part in parts:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data.encode("UTF-8")).decode("utf-8", errors="ignore")

    for part in parts:
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data", "")
            if data:
                html = base64.urlsafe_b64decode(data.encode(
                    "UTF-8")).decode("utf-8", errors="ignore")
                return re.sub(r"<[^>]+>", " ", html)

    return ""


def list_unread_emails(gmail_service, max_results: int = 5) -> List[Dict]:
    results = gmail_service.users().messages().list(
        userId="me",
        labelIds=["UNREAD"],
        maxResults=max_results
    ).execute()

    messages = results.get("messages", [])
    emails = []

    for msg in messages:
        detail = gmail_service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="full"
        ).execute()

        headers = detail.get("payload", {}).get("headers", [])
        header_map = {h["name"].lower(): h["value"] for h in headers}

        sender = decode_mime_text(header_map.get("from", ""))
        subject = decode_mime_text(header_map.get("subject", ""))
        date_value = decode_mime_text(header_map.get("date", ""))
        snippet = detail.get("snippet", "")
        body = extract_email_body(detail.get("payload", {}))

        emails.append({
            "id": msg["id"],
            "threadId": detail.get("threadId", ""),
            "sender": sender,
            "subject": subject,
            "date": date_value,
            "snippet": snippet,
            "body": body,
            "payload": detail.get("payload", {}),
        })

    return emails


def classify_intent(client: OpenAI, sender: str, subject: str, snippet: str, body: str) -> str:
    prompt = f"""
Classify this email into exactly one label:
Invoice, Meeting Request, Spam, Action Required

Sender: {sender}
Subject: {subject}
Snippet: {snippet}
Body: {body[:3500]}

Return only the label.
"""
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You are an email intent classifier. Return only one label."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
    )
    label = (response.choices[0].message.content or "").strip()
    if label not in INTENTS:
        return "Action Required"
    return label


def generate_reply(client: OpenAI, sender: str, subject: str, intent: str, body: str) -> str:
    prompt = f"""
Write a professional customer support email reply.

Intent: {intent}
Sender: {sender}
Subject: {subject}
Email body: {body[:3500]}

Rules:
- Keep it short, polite, and useful.
- If intent is Invoice, acknowledge receipt and mention the file is being processed.
- If Meeting Request, confirm receipt and ask for preferred time if missing.
- If Spam, politely indicate no action is needed.
- If Action Required, respond helpfully and request any missing details.
- Do not mention internal classification.
"""
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You draft concise, professional email replies."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )
    return (response.choices[0].message.content or "").strip()


def create_message(to: str, subject: str, body_text: str, reply_to: str = "", thread_id: str = "") -> Dict:
    message = MIMEText(body_text)
    message["to"] = to
    if reply_to:
        message["In-Reply-To"] = reply_to
        message["References"] = reply_to
    message["subject"] = f"Re: {subject}" if not subject.lower(
    ).startswith("re:") else subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    return payload


def send_message(gmail_service, user_id: str, message: Dict) -> Dict:
    return gmail_service.users().messages().send(
        userId=user_id,
        body=message
    ).execute()


def extract_pdf_attachment_id(payload: Dict) -> Optional[str]:
    parts = payload.get("parts", [])
    for part in parts:
        filename = part.get("filename", "")
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        if filename and (filename.lower().endswith(".pdf") or mime_type == "application/pdf"):
            attachment_id = body.get("attachmentId")
            if attachment_id:
                return attachment_id
    return None


def download_attachment(gmail_service, message_id: str, attachment_id: str, file_name: str) -> str:
    attachment = gmail_service.users().messages().attachments().get(
        userId="me",
        messageId=message_id,
        id=attachment_id
    ).execute()

    data = attachment["data"]
    file_data = base64.urlsafe_b64decode(data.encode("UTF-8"))

    path = os.path.join(DOWNLOAD_DIR, file_name)
    with open(path, "wb") as f:
        f.write(file_data)
    return path


def sanitize_name(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def extract_vendor(subject: str, sender: str, body: str) -> str:
    combined = f"{subject} {sender} {body}"
    patterns = [
        r"invoice\s+from\s+([A-Za-z0-9&\-\s]{2,40})",
        r"bill\s+from\s+([A-Za-z0-9&\-\s]{2,40})",
        r"receipt\s+from\s+([A-Za-z0-9&\-\s]{2,40})",
    ]
    for pattern in patterns:
        m = re.search(pattern, combined, re.IGNORECASE)
        if m:
            return sanitize_name(m.group(1).strip()[:30])

    sender_clean = sender.split("<")[0].strip()
    sender_clean = re.sub(r"[^A-Za-z0-9\s&-]", "", sender_clean)
    return sanitize_name(sender_clean[:30]) or "Vendor"


def extract_email_date_short(raw_date: str) -> str:
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw_date)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


def upload_to_drive(drive_service, file_path: str, folder_id: str) -> str:
    metadata = {"name": os.path.basename(file_path)}
    if folder_id:
        metadata["parents"] = [folder_id]

    media = MediaFileUpload(file_path, mimetype="application/pdf")
    file_obj = drive_service.files().create(
        body=metadata,
        media_body=media,
        fields="id, name, webViewLink"
    ).execute()
    return file_obj.get("webViewLink", "")


def mark_message_as_processed(gmail_service, message_id: str):
    try:
        gmail_service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except Exception:
        pass


def init_state():
    if "emails" not in st.session_state:
        st.session_state.emails = []
    if "results" not in st.session_state:
        st.session_state.results = {}
    if "actions" not in st.session_state:
        st.session_state.actions = {}


def main():
    st.set_page_config(page_title="Email Inbox Actioner",
                       page_icon="📧", layout="wide")
    st.title("📧 Email Inbox Actioner")
    st.caption(
        "Human-in-the-loop Gmail triage with safe sending and Drive uploads")

    init_db()
    init_state()
    client = get_openai_client()

    if client is None:
        st.error("OPENAI_API_KEY is missing.")
        return

    try:
        gmail_service, drive_service = gmail_auth()
    except Exception as e:
        st.error(f"Google auth error: {e}")
        return

    top_bar = st.columns([1, 1, 2])
    with top_bar[0]:
        fetch_clicked = st.button(
            "Fetch 5 unread emails", use_container_width=True)
    with top_bar[1]:
        clear_clicked = st.button("Clear view", use_container_width=True)
    with top_bar[2]:
        st.write(f"Drive folder: `{GOOGLE_DRIVE_FOLDER_ID or 'Not set'}`")

    if clear_clicked:
        st.session_state.emails = []
        st.session_state.results = {}
        st.session_state.actions = {}
        st.rerun()

    if fetch_clicked:
        try:
            st.session_state.emails = list_unread_emails(
                gmail_service, max_results=5)
            st.session_state.results = {}
            st.session_state.actions = {}
            if not st.session_state.emails:
                st.info("No unread emails found.")
        except HttpError as e:
            st.error(f"Gmail API error: {e}")
            return
        except Exception as e:
            st.error(f"Unexpected fetch error: {e}")
            return

    if not st.session_state.emails:
        st.info("Click 'Fetch 5 unread emails' to begin.")
        return

    for idx, email_item in enumerate(st.session_state.emails):
        with st.container(border=True):
            st.subheader(email_item["subject"] or "(No subject)")
            st.write(f"**From:** {email_item['sender']}")
            st.write(f"**Date:** {email_item['date']}")
            st.write(f"**Snippet:** {email_item['snippet']}")

            key_prefix = email_item["id"]
            if key_prefix not in st.session_state.results:
                try:
                    intent = classify_intent(
                        client,
                        email_item["sender"],
                        email_item["subject"],
                        email_item["snippet"],
                        email_item["body"],
                    )
                    reply_text = generate_reply(
                        client,
                        email_item["sender"],
                        email_item["subject"],
                        intent,
                        email_item["body"],
                    )
                    st.session_state.results[key_prefix] = {
                        "intent": intent,
                        "reply": reply_text,
                    }
                    log_action(
                        email_item["id"],
                        email_item["sender"],
                        email_item["subject"],
                        intent,
                        "classified",
                        "",
                        status="classified",
                    )
                except Exception as e:
                    st.error(f"Classification error: {e}")
                    continue

            intent = st.session_state.results[key_prefix]["intent"]
            reply_text = st.session_state.results[key_prefix]["reply"]

            st.write(f"**Intent:** {intent}")
            st.write("**Suggested reply:**")
            st.code(reply_text, language="text")

            c1, c2, c3 = st.columns(3)

            with c1:
                approve_reply = st.button(
                    "Approve & Send Reply",
                    key=f"send_{key_prefix}",
                    use_container_width=True
                )

            with c2:
                approve_invoice = st.button(
                    "Approve Invoice Action",
                    key=f"invoice_{key_prefix}",
                    use_container_width=True,
                    disabled=intent != "Invoice"
                )

            with c3:
                skip_email = st.button(
                    "Skip",
                    key=f"skip_{key_prefix}",
                    use_container_width=True
                )

            if approve_reply:
                try:
                    sender_email = re.search(
                        r"<([^>]+)>", email_item["sender"])
                    to_email = sender_email.group(
                        1) if sender_email else email_item["sender"].split(" ")[-1].strip()
                    message = create_message(
                        to=to_email,
                        subject=email_item["subject"],
                        body_text=reply_text,
                        thread_id=email_item.get("threadId", "")
                    )
                    sent = send_message(gmail_service, "me", message)
                    mark_message_as_processed(gmail_service, email_item["id"])
                    st.success(f"Reply sent. Message ID: {sent.get('id', '')}")
                    log_action(
                        email_item["id"],
                        email_item["sender"],
                        email_item["subject"],
                        intent,
                        "send_reply",
                        "",
                        reply_sent=1,
                        status="reply_sent",
                    )
                except Exception as e:
                    st.error(f"Failed to send reply: {e}")

            if approve_invoice and intent == "Invoice":
                try:
                    attachment_id = extract_pdf_attachment_id(
                        email_item["payload"])
                    if not attachment_id:
                        st.warning("No PDF attachment found.")
                        log_action(
                            email_item["id"],
                            email_item["sender"],
                            email_item["subject"],
                            intent,
                            "invoice_upload",
                            "",
                            status="missing_pdf",
                        )
                    else:
                        vendor = extract_vendor(
                            email_item["subject"], email_item["sender"], email_item["body"])
                        date_short = extract_email_date_short(
                            email_item["date"])
                        final_name = sanitize_name(
                            f"Invoice_{vendor}_{date_short}.pdf")
                        local_path = download_attachment(
                            gmail_service,
                            email_item["id"],
                            attachment_id,
                            final_name
                        )
                        st.success(f"Downloaded: {local_path}")
                        link = upload_to_drive(
                            drive_service, local_path, GOOGLE_DRIVE_FOLDER_ID)
                        st.success(
                            f"Uploaded to Drive: {link or 'Upload complete'}")
                        mark_message_as_processed(
                            gmail_service, email_item["id"])
                        log_action(
                            email_item["id"],
                            email_item["sender"],
                            email_item["subject"],
                            intent,
                            "invoice_upload",
                            final_name,
                            drive_uploaded=1,
                            status="approved_uploaded",
                        )
                except Exception as e:
                    st.error(f"Invoice action failed: {e}")

            if skip_email:
                st.info("Skipped by user.")
                log_action(
                    email_item["id"],
                    email_item["sender"],
                    email_item["subject"],
                    intent,
                    "skip",
                    "",
                    status="skipped",
                )

            with st.expander("Debug details"):
                st.json(email_item)


if __name__ == "__main__":
    main()
