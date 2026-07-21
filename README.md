# Email Inbox Actioner

A human-in-the-loop Gmail triage app built with Python, Streamlit, OpenAI, Gmail API, and Google Drive API.

## Overview

This project reads unread emails from Gmail, classifies them into:

- Invoice
- Meeting Request
- Spam
- Action Required

For invoice emails, it can download the PDF attachment, rename it, and upload it to Google Drive after user approval.
For replies, the app drafts an email and sends it directly only after the user clicks approval.

## Features

- Fetch the 5 most recent unread emails.
- Classify email intent with an LLM.
- Draft professional replies.
- Send replies directly after approval.
- Download invoice PDF attachments.
- Rename invoice files using vendor and date.
- Upload files to Google Drive after approval.
- Log actions in SQLite.
- Safe human-in-the-loop automation.

## Tech Stack

- Python
- Streamlit
- OpenAI API
- Gmail API
- Google Drive API
- SQLite

## Project Structure

```text
project 3/
├─ app.py
├─ README.md
├─ requirements.txt
├─ .gitignore
├─ LICENSE
├─ .env
├─ credentials.json
├─ token.json
├─ triage_logs.db
├─ downloads/
└─ chroma_db/
```

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

1. Add your API keys in `.env`:

```env
OPENAI_API_KEY=your_openai_key_here
OPENAI_MODEL=gpt-4o-mini
GOOGLE_DRIVE_FOLDER_ID=your_folder_id_here
```

1. Put your Google OAuth file as `credentials.json`.
2. Run the app:

```powershell
streamlit run app.py
```

## Google Setup

- Enable Gmail API in Google Cloud.
- Enable Google Drive API in Google Cloud.
- Create a desktop OAuth client.
- Add your Gmail address as a test user.
- Allow the app access during sign-in.

## Safety

This app uses a human approval step before sending replies or uploading invoice files.

## License

MIT License
