<h1 align="center">Complaint Machine</h1>
<p align="center">Drafts a firm, well-sourced complaint letter and tells you exactly where to file it. Nothing is sent without your approval.</p>
<p align="center">
  <a href="#how-it-works">How it works</a> ·
  <a href="#run-locally">Run locally</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#status">Status</a>
</p>
<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-111318?style=flat-square" alt="MIT license">
  <img src="https://img.shields.io/badge/Python-3.9%2B-111318?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/Flask-3-111318?style=flat-square&logo=flask&logoColor=white" alt="Flask 3">
</p>

## Why

Most complaints fail on the first letter: vague, angry, or sent to an address nobody reads. Complaint Machine drafts the letter from your facts, suggests where to send it, and drafts the text for the official regulator and BBB forms. A human reads and approves every outbound action.

## How it works

1. You enter the company, what happened, the outcome you want, optional evidence and a tone: firm, assertive, legal or polite. A checkbox marks financial products.
2. Llama 3.3 70B on Novita AI (OpenAI-compatible API) drafts the letter. The prompt tells the model to use only your facts and to avoid statute numbers it cannot be sure of. It leaves `[YOUR NAME]`, `[YOUR EMAIL]` and `[YOUR PHONE]` placeholders for you to fill in.
3. The model also suggests contacts: a customer service email, up to 5 executive emails, an X handle and a phone number. The server drops malformed values. Every suggestion is labeled "unverified suggestion" in the UI and in the API response (`contacts_status`).
4. Email and tweet go through ActionLayer, one recipient or one post at a time. You edit the text, click Review, and the UI shows the exact recipient and text. Only after you approve does the POST fire.
5. For regulators and review bodies, the app drafts text tailored to each venue and shows the official page. You paste it in and submit it yourself.

| Venue | Shown when | Official page |
|-------|------------|---------------|
| Better Business Bureau | Always | https://www.bbb.org/file-a-complaint |
| CFPB | You tick the financial product box | https://www.consumerfinance.gov/complaint/ |
| State attorney general | Always | https://www.naag.org/find-my-ag/ (NAAG directory, then your state's form) |
| FTC ReportFraud | Always | https://reportfraud.ftc.gov/ |

### Approval gate

The send routes enforce the approval step on the server, not only in the UI. A request is rejected with `400` and nothing is sent unless it carries `"confirmed": true` and the exact text to send. The server never falls back to a stored letter or a suggested recipient. It also rejects a message that still contains an unfilled `[YOUR ...]` placeholder.

```bash
curl -X POST http://localhost:5000/api/complaint/<id>/send-email \
  -H "Content-Type: application/json" \
  -d '{"confirmed": true, "email": "support@example.com", "subject": "Complaint regarding Acme", "text": "Dear Acme, ..."}'
```

```json
{"success": true, "sent": {"type": "email", "to": "support@example.com", "subject": "Complaint regarding Acme", "text": "Dear Acme, ..."}, "result": {"success": true, "result": {}}}
```

| Route | Purpose |
|-------|---------|
| `POST /api/complaint` | Draft the letter, contact suggestions and venue list |
| `GET /api/complaint/<id>` | Read a complaint held in server memory |
| `POST /api/complaint/<id>/draft/venue/<venue>` | Draft text for `bbb`, `cfpb`, `state_ag` or `ftc` |
| `POST /api/complaint/<id>/draft/tweet` | Draft one tweet of 280 characters or fewer |
| `POST /api/complaint/<id>/draft/follow-up` | Draft a follow-up email. Does not send it |
| `POST /api/complaint/<id>/send-email` | Send one email. Requires `confirmed: true` |
| `POST /api/complaint/<id>/tweet` | Post one tweet. Requires `confirmed: true` |

Drafting and sending each allow 10 requests per minute per client IP. Beyond that the server returns `429` with a `Retry-After` header.

## Run locally

Prerequisites: Python 3.9 or newer, a [Novita AI](https://novita.ai) key, and optionally an [ActionLayer](https://actionlayer.io) key for sending.

```bash
git clone https://github.com/elsisiem/complaint-machine.git
cd complaint-machine
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # add your keys
python app.py
```

Open http://localhost:5000. Without an ActionLayer key, drafting works and the send routes return `503`.

## Configuration

Set these in `.env` or in the host's environment. `.env.example` lists the same variables.

| Variable | Required | Purpose |
|----------|----------|---------|
| `NOVITA_API_KEY` | Yes | Novita AI key for letters, venue text and contact suggestions |
| `ACTIONLAYER_API_KEY` | For sending | ActionLayer key for the approved email or tweet |
| `PORT` | No | Port to listen on. Defaults to 5000 |
| `FLASK_DEBUG` | No | Set to `1` to run the Flask debugger. Never on a public host |
| `TRUST_PROXY` | No | Set to `1` behind a reverse proxy so the rate limit sees the real client IP |

The app binds to `0.0.0.0`.

## Deploy

Render: create a new Blueprint and point it at this repo. `render.yaml` defines one web service that runs `pip install -r requirements.txt` and `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120`. Render provides `PORT`. Set `NOVITA_API_KEY` and `ACTIONLAYER_API_KEY` in the dashboard. The blueprint sets `TRUST_PROXY=1`.

The `Procfile` runs the same gunicorn command for other Procfile hosts. Use one worker and several threads, not several workers: complaints and rate limit counters live in process memory, so a second worker would not see complaints created by the first. `python app.py` (the Flask development server) is for local use only.

## Project layout

```
app.py                 Routes, prompts, rate limit, Novita and ActionLayer clients
templates/index.html   Single-page UI (Tailwind CDN, vanilla JS)
render.yaml            Render blueprint
Procfile               gunicorn start command for Procfile hosts
.env.example           Environment variable template
requirements.txt       Flask, requests, python-dotenv, gunicorn
```

## Status

- Contacts are LLM guesses. The model can invent addresses and handles. Verify each one on the company's official site before you send anything.
- No persistence. Complaints live in server memory (the newest 500 are kept) and are lost on restart. The session list in the page is also in memory.
- No authentication. Anyone who can open a deployed instance can draft with your Novita key and, with an ActionLayer key set, send an email or tweet from the account connected there. The rate limit slows this down and does not prevent it. Keep a deployment private or put access control in front of it.
- ActionLayer `success` means it accepted the task. The app does not confirm delivery.
- Drafts are model output, not legal advice. Read them before you use them.
- Production runs under gunicorn with one worker, so it cannot scale out across processes. There are no automated tests.

## Credits

Built on Novita AI for the model and ActionLayer for agent actions.

## License

MIT. See [LICENSE](LICENSE).
