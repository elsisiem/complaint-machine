import os
import re
import json
import time
import secrets
import threading
from collections import OrderedDict, deque
from datetime import datetime, timezone
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

if os.getenv("TRUST_PROXY") == "1":
    # Behind a reverse proxy (Render, Fly, nginx) the client IP is in X-Forwarded-For.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

NOVITA_API_KEY = os.getenv("NOVITA_API_KEY")
NOVITA_BASE_URL = "https://api.novita.ai/openai/v1"
NOVITA_MODEL = "meta-llama/llama-3.3-70b-instruct"
ACTIONLAYER_API_KEY = os.getenv("ACTIONLAYER_API_KEY")
ACTIONLAYER_BASE_URL = "https://api.actionlayer.io"

MAX_COMPLAINTS = 500
RATE_LIMIT = 10
RATE_WINDOW = 60

UNVERIFIED = "unverified suggestion"
CONTACTS_NOTE = (
    "Suggested by an LLM and not checked against any source. "
    "Confirm the address on the company's official website before you send anything."
)

complaints_db = OrderedDict()

TONES = {
    "firm": "Professional, firm, and factual. Refer to consumer protection law in general terms.",
    "assertive": (
        "Direct and assertive. State plainly that you will file with the BBB and your state "
        "attorney general if the issue is not resolved. No insults."
    ),
    "legal": (
        "Formal legal language. Refer to applicable consumer protection law in general terms. "
        "Do not cite statute numbers or case law unless you are certain they apply."
    ),
    "polite": (
        "Courteous and cooperative. Assume good faith, explain the problem clearly, "
        "and ask plainly for the fix you want."
    ),
}

VENUES = {
    "bbb": {
        "name": "Better Business Bureau",
        "url": "https://www.bbb.org/file-a-complaint",
        "note": "BBB forwards your complaint to the business and asks it to respond.",
        "financial_only": False,
        "guidance": (
            "Write the description for a Better Business Bureau complaint form. Neutral and "
            "factual, first person. Give a short timeline, any order or account numbers from "
            "the evidence, and the resolution you are asking for. Under 250 words."
        ),
    },
    "cfpb": {
        "name": "Consumer Financial Protection Bureau",
        "url": "https://www.consumerfinance.gov/complaint/",
        "note": "For banks, lenders, credit cards, credit reporting and other financial products.",
        "financial_only": True,
        "guidance": (
            "Write the narrative for a CFPB complaint about a financial product or service. "
            "First person. Cover what happened and when, the product involved, what you already "
            "tried with the company, and the fix you want. No legal citations. Under 300 words."
        ),
    },
    "state_ag": {
        "name": "State attorney general",
        "url": "https://www.naag.org/find-my-ag/",
        "note": "NAAG's directory links to your state's office. Each office has its own complaint form.",
        "financial_only": False,
        "guidance": (
            "Write a consumer complaint to a state attorney general's office. First person and "
            "factual. Describe the business practice you believe is unfair or deceptive, what "
            "happened, any money lost, and the resolution you want. Do not cite statute numbers. "
            "Under 300 words."
        ),
    },
    "ftc": {
        "name": "FTC ReportFraud",
        "url": "https://reportfraud.ftc.gov/",
        "note": "The FTC uses reports to spot patterns. It does not resolve individual disputes.",
        "financial_only": False,
        "guidance": (
            "Write the description for an FTC ReportFraud report about a deceptive or unfair "
            "business practice. First person. State what the business did or promised, how you "
            "paid or were contacted, any amount lost, and the dates. Under 250 words."
        ),
    },
}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
PHONE_RE = re.compile(r"^[0-9+()\-.\s]{7,25}$")
PLACEHOLDER_RE = re.compile(r"\[\s*your\s[^\]]{1,30}\]", re.IGNORECASE)
NULL_WORDS = {"null", "none", "n/a", "unknown"}


class ServiceError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Rate limiting: in-memory, per client IP, per scope. Resets on restart.
# ---------------------------------------------------------------------------

_hits = {}
_hits_lock = threading.Lock()


def client_ip():
    return request.remote_addr or "unknown"


def rate_limited(scope):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            now = time.monotonic()
            key = (scope, client_ip())
            with _hits_lock:
                if len(_hits) > 5000:
                    for k in [k for k, q in _hits.items() if not q or now - q[-1] > RATE_WINDOW]:
                        del _hits[k]
                hits = _hits.setdefault(key, deque())
                while hits and now - hits[0] > RATE_WINDOW:
                    hits.popleft()
                if len(hits) >= RATE_LIMIT:
                    retry_after = int(RATE_WINDOW - (now - hits[0])) + 1
                    response = jsonify({"error": f"Rate limit: {RATE_LIMIT} requests per minute. Try again shortly."})
                    response.status_code = 429
                    response.headers["Retry-After"] = str(retry_after)
                    return response
                hits.append(now)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# LLM and ActionLayer clients
# ---------------------------------------------------------------------------

def novita_chat(prompt, max_tokens=1500, temperature=0.7):
    if not NOVITA_API_KEY:
        raise ServiceError("NOVITA_API_KEY is not set on the server.", 503)
    try:
        response = requests.post(
            f"{NOVITA_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {NOVITA_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": NOVITA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=60,
        )
    except requests.RequestException as e:
        raise ServiceError(f"Could not reach Novita: {e}")
    if response.status_code == 200:
        try:
            return response.json()["choices"][0]["message"]["content"].strip()
        except (ValueError, KeyError, IndexError, TypeError):
            raise ServiceError("Novita returned an unexpected response.")
    try:
        error_msg = response.json().get("message", response.text)
    except ValueError:
        error_msg = response.text
    if "NOT_ENOUGH_BALANCE" in str(error_msg):
        raise ServiceError("Novita API: insufficient balance. Add credits at novita.ai/billing.", 402)
    raise ServiceError(f"Novita API error: {error_msg}")


def execute_actionlayer(goal):
    headers = {"Authorization": f"Bearer {ACTIONLAYER_API_KEY}", "Content-Type": "application/json"}
    try:
        response = requests.post(f"{ACTIONLAYER_BASE_URL}/tasks", headers=headers, json={"goal": goal}, timeout=30)
        if response.status_code == 200:
            return {"success": True, "result": response.json()}
        return {"success": False, "error": response.text}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

def generate_complaint_letter(company, violation, desired_outcome, evidence, tone):
    prompt = f"""Write a complaint letter that will get results.

COMPANY: {company}
WHAT HAPPENED: {violation}
DESIRED OUTCOME: {desired_outcome}
EVIDENCE: {evidence or "none provided"}
TONE: {TONES[tone]}

RULES:
1. Clear subject line
2. State the problem factually in the first paragraph
3. Use only the facts above. Do not invent dates, amounts, names or reference numbers
4. Do not cite statute numbers unless you are certain they apply
5. List specific damages or inconvenience
6. State exactly what you want
7. Set a 7-day deadline
8. Say you will file with the BBB and your state attorney general if it is unresolved
9. End with "I look forward to your prompt resolution"
10. Under 400 words
11. Include [YOUR NAME], [YOUR EMAIL], [YOUR PHONE] placeholders

Write ONLY the letter."""
    return novita_chat(prompt, 1500, 0.7)


def clean_contacts(raw):
    """Keep only well-formed values from the LLM's guess. Nothing here is verified."""
    out = {"cs_email": None, "exec_emails": [], "twitter": None, "phone": None}
    if not isinstance(raw, dict):
        return out

    def email(value):
        if isinstance(value, str):
            value = value.strip()
            if EMAIL_RE.match(value):
                return value
        return None

    out["cs_email"] = email(raw.get("cs_email"))
    execs = raw.get("exec_emails")
    if isinstance(execs, list):
        for item in execs:
            addr = email(item)
            if addr and addr not in out["exec_emails"]:
                out["exec_emails"].append(addr)
        out["exec_emails"] = out["exec_emails"][:5]
    twitter = raw.get("twitter")
    if isinstance(twitter, str) and twitter.strip().lower() not in NULL_WORDS:
        match = HANDLE_RE.match(twitter.strip())
        if match:
            out["twitter"] = "@" + match.group(1)
    phone = raw.get("phone")
    if isinstance(phone, str) and PHONE_RE.match(phone.strip()):
        out["phone"] = phone.strip()
    return out


def find_company_contacts(company):
    prompt = f"""List contact details for {company}.
Return ONLY valid JSON with these keys:
{{
  "cs_email": "customer service email or null",
  "exec_emails": ["executive emails, empty list if unsure"],
  "twitter": "@handle or null",
  "phone": "customer service phone or null"
}}

Use null or an empty list for anything you are not confident is real.
Do not guess addresses from common patterns."""
    try:
        content = novita_chat(prompt, 500, 0.2)
        return clean_contacts(json.loads(content[content.index("{"):content.rindex("}") + 1]))
    except Exception:
        return clean_contacts(None)


def venues_for(complaint):
    return [
        {"id": vid, "name": v["name"], "url": v["url"], "note": v["note"]}
        for vid, v in VENUES.items()
        if complaint["financial"] or not v["financial_only"]
    ]


def draft_venue_text(complaint, venue):
    prompt = f"""{venue["guidance"]}

COMPANY: {complaint["company"]}
WHAT HAPPENED: {complaint["violation"]}
DESIRED OUTCOME: {complaint["desired_outcome"]}
EVIDENCE: {complaint["evidence"] or "none provided"}

Use only the facts above. Do not invent dates, amounts, names or reference numbers.
Where a detail is missing, write a bracketed placeholder such as [DATE] or [AMOUNT].
Write ONLY the text to paste into the form."""
    return novita_chat(prompt, 700, 0.4)


def draft_tweet_text(complaint):
    prompt = f"""Write one tweet of at most 270 characters about this complaint against {complaint["company"]}.
What happened: {complaint["violation"]}
Be factual and specific. No insults, no hashtags, no handles.
Write ONLY the tweet."""
    text = novita_chat(prompt, 200, 0.6).strip().strip('"')
    if len(text) > 280:
        text = text[:277].rsplit(" ", 1)[0] + "..."
    return text


def draft_follow_up_text(complaint, attempt):
    prompt = f"""Write follow-up #{attempt} to {complaint["company"]} about an unresolved complaint.
Original letter: {complaint["letter"][:800]}
Refer to the earlier letter, restate what you want, and give a new 7-day deadline.
Remain firm and courteous. Say you will file with the BBB and your state attorney general if it is unresolved.
Under 200 words. Sign with [YOUR NAME].
Write ONLY the message."""
    return novita_chat(prompt, 600, 0.6)


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

def json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def read_text(data, key, max_len, required=True):
    value = data.get(key, "")
    if not isinstance(value, str):
        return None, f"'{key}' must be a string."
    value = value.strip()
    if required and not value:
        return None, f"'{key}' is required."
    if len(value) > max_len:
        return None, f"'{key}' is longer than {max_len} characters."
    return value, None


def get_or_404(complaint_id):
    complaint = complaints_db.get(complaint_id)
    if complaint is None:
        return None, (jsonify({"error": "Not found"}), 404)
    return complaint, None


def approved_text(data, max_len):
    """Validate the approval gate. Returns (text, error_response)."""
    if data.get("confirmed") is not True:
        return None, (jsonify({
            "error": "Nothing was sent. Approval is required: include \"confirmed\": true and the exact text to send."
        }), 400)
    text, err = read_text(data, "text", max_len)
    if err:
        return None, (jsonify({"error": f"Nothing was sent. {err}"}), 400)
    return text, None


def dispatch(complaint, kind, target, goal, content, subject=None):
    if not ACTIONLAYER_API_KEY:
        return jsonify({"success": False, "error": "ACTIONLAYER_API_KEY is not set on the server."}), 503
    result = execute_actionlayer(goal)
    complaint["actions"].append({
        "type": kind,
        "target": target,
        "subject": subject,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result": result,
    })
    if result["success"] and kind == "email":
        complaint["status"] = "sent"
    sent = {"type": kind, "to": target, "subject": subject, "text": content}
    return jsonify({"success": result["success"], "sent": sent, "result": result}), (200 if result["success"] else 502)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/complaint", methods=["POST"])
@rate_limited("generate")
def create_complaint():
    data = json_body()
    fields = {}
    for key, limit, required in (
        ("company", 200, True),
        ("violation", 4000, True),
        ("desired_outcome", 1000, True),
        ("evidence", 4000, False),
    ):
        value, err = read_text(data, key, limit, required)
        if err:
            return jsonify({"error": err}), 400
        fields[key] = value
    tone = data.get("tone", "firm")
    if tone not in TONES:
        return jsonify({"error": f"'tone' must be one of: {', '.join(TONES)}."}), 400

    try:
        letter = generate_complaint_letter(
            fields["company"], fields["violation"], fields["desired_outcome"], fields["evidence"], tone
        )
    except ServiceError as e:
        return jsonify({"error": str(e)}), e.status
    contacts = find_company_contacts(fields["company"])

    complaint_id = secrets.token_urlsafe(8)
    complaint = {
        "id": complaint_id,
        **fields,
        "tone": tone,
        "financial": data.get("financial") is True,
        "letter": letter,
        "contacts": contacts,
        "status": "draft",
        "drafts": {},
        "follow_ups": 0,
        "actions": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    complaints_db[complaint_id] = complaint
    while len(complaints_db) > MAX_COMPLAINTS:
        complaints_db.popitem(last=False)

    return jsonify({
        "complaint_id": complaint_id,
        "letter": letter,
        "contacts": contacts,
        "contacts_status": UNVERIFIED,
        "contacts_note": CONTACTS_NOTE,
        "venues": venues_for(complaint),
    })


@app.route("/api/complaint/<complaint_id>", methods=["GET"])
def get_complaint(complaint_id):
    complaint, err = get_or_404(complaint_id)
    if err:
        return err
    return jsonify({
        **complaint,
        "contacts_status": UNVERIFIED,
        "contacts_note": CONTACTS_NOTE,
        "venues": venues_for(complaint),
    })


@app.route("/api/complaint/<complaint_id>/draft/venue/<venue_id>", methods=["POST"])
@rate_limited("generate")
def draft_venue(complaint_id, venue_id):
    complaint, err = get_or_404(complaint_id)
    if err:
        return err
    venue = VENUES.get(venue_id)
    if venue is None:
        return jsonify({"error": "Unknown venue."}), 404
    if venue["financial_only"] and not complaint["financial"]:
        return jsonify({"error": f"{venue['name']} applies to financial products only."}), 400
    try:
        text = draft_venue_text(complaint, venue)
    except ServiceError as e:
        return jsonify({"error": str(e)}), e.status
    complaint["drafts"][venue_id] = text
    return jsonify({"venue": venue_id, "name": venue["name"], "url": venue["url"], "note": venue["note"], "draft": text})


@app.route("/api/complaint/<complaint_id>/draft/tweet", methods=["POST"])
@rate_limited("generate")
def draft_tweet(complaint_id):
    complaint, err = get_or_404(complaint_id)
    if err:
        return err
    try:
        text = draft_tweet_text(complaint)
    except ServiceError as e:
        return jsonify({"error": str(e)}), e.status
    return jsonify({"draft": text})


@app.route("/api/complaint/<complaint_id>/draft/follow-up", methods=["POST"])
@rate_limited("generate")
def draft_follow_up(complaint_id):
    complaint, err = get_or_404(complaint_id)
    if err:
        return err
    attempt = complaint["follow_ups"] + 1
    try:
        text = draft_follow_up_text(complaint, attempt)
    except ServiceError as e:
        return jsonify({"error": str(e)}), e.status
    complaint["follow_ups"] = attempt
    return jsonify({"draft": text, "attempt": attempt})


@app.route("/api/complaint/<complaint_id>/send-email", methods=["POST"])
@rate_limited("send")
def send_email(complaint_id):
    data = json_body()
    text, err = approved_text(data, 10000)
    if err:
        return err
    recipient, e1 = read_text(data, "email", 254)
    subject, e2 = read_text(data, "subject", 200)
    if e1 or e2 or not EMAIL_RE.match(recipient or ""):
        return jsonify({"error": "Nothing was sent. A valid recipient 'email' and a 'subject' are required."}), 400
    if "\n" in subject or "\r" in subject:
        return jsonify({"error": "Nothing was sent. The subject must be a single line."}), 400
    placeholders = sorted({m.group(0) for m in PLACEHOLDER_RE.finditer(text)})
    if placeholders:
        return jsonify({"error": f"Nothing was sent. Fill in these placeholders first: {', '.join(placeholders)}"}), 400
    complaint, err = get_or_404(complaint_id)
    if err:
        return err
    goal = (
        f"Send exactly one email. To: {recipient}. Subject: {subject}. "
        f"Send the body below verbatim. Do not change it and do not contact anyone else.\n\nBody:\n{text}"
    )
    return dispatch(complaint, "email", recipient, goal, text, subject)


@app.route("/api/complaint/<complaint_id>/tweet", methods=["POST"])
@rate_limited("send")
def tweet_complaint(complaint_id):
    data = json_body()
    text, err = approved_text(data, 280)
    if err:
        return err
    complaint, err = get_or_404(complaint_id)
    if err:
        return err
    goal = f"Post exactly one tweet with this text, verbatim, and do nothing else: {text}"
    return dispatch(complaint, "tweet", None, goal, text)


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT") or 5000),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )
