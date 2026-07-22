import os
import json
import time
import requests
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

NOVITA_API_KEY = os.getenv("NOVITA_API_KEY")
NOVITA_BASE_URL = "https://api.novita.ai/openai/v1"
ACTIONLAYER_API_KEY = os.getenv("ACTIONLAYER_API_KEY")
ACTIONLAYER_BASE_URL = "https://api.actionlayer.io"

complaints_db = {}
credentials_db = {
    "email": None,
    "twitter": None,
    "linkedin": None
}

def novita_chat(prompt, max_tokens=1500, temperature=0.7):
    headers = {
        "Authorization": f"Bearer {NOVITA_API_KEY}",
        "Content-Type": "application/json"
    }
    response = requests.post(
        f"{NOVITA_BASE_URL}/chat/completions",
        headers=headers,
        json={
            "model": "meta-llama/llama-3.3-70b-instruct",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature
        }
    )
    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"]
    else:
        error_data = response.json() if response.headers.get('content-type') == 'application/json' else {}
        error_msg = error_data.get('message', response.text)
        if 'NOT_ENOUGH_BALANCE' in str(error_msg):
            raise Exception("Novita API: Insufficient balance. Add credits at novita.ai/billing")
        raise Exception(f"Novita API error: {error_msg}")

def generate_complaint_letter(company, violation, desired_outcome, evidence, tone="firm"):
    tone_instructions = {
        "firm": "Professional, firm, and factual. Cite consumer protection laws where relevant.",
        "aggressive": "Assertive and direct. Make clear you will escalate to regulatory bodies and legal action if needed.",
        "pathetic": "Emotional and sympathetic. Appeal to their humanity and customer service values.",
        "legal": "Formal legal language. Reference specific statutes, regulations, and potential legal consequences."
    }
    prompt = f"""Write a complaint letter that will get results.

COMPANY: {company}
WHAT HAPPENED: {violation}
DESIRED OUTCOME: {desired_outcome}
EVIDENCE: {evidence}
TONE: {tone_instructions.get(tone, tone_instructions['firm'])}

RULES:
1. Clear subject line
2. State problem factually in first paragraph
3. Reference consumer protection laws (CCPA, FTC, state regulations)
4. List specific damages/inconvenience
5. State exactly what you want
6. Set 7-day deadline
7. Mention escalation to BBB, AG, social media if unresolved
8. End with "I look forward to your prompt resolution"
9. Under 400 words
10. Include [YOUR NAME], [YOUR EMAIL], [YOUR PHONE] placeholders

Write ONLY the letter."""

    return novita_chat(prompt, 1500, 0.7)

def find_company_contacts(company):
    prompt = f"""Research {company} and provide their contact info.
Return ONLY valid JSON with these keys:
{{
  "cs_email": "customer service email or null",
  "exec_emails": ["list of executive emails you can find"],
  "twitter": "@twitter handle or null",
  "linkedin": "linkedin company page URL or null",
  "headquarters": "address or null",
  "bbb_url": "BBB profile URL or null",
  "phone": "customer service phone or null"
}}

Be specific. Use real emails if you know them. Common patterns: support@company.com, customercare@company.com, ceo@company.com."""

    try:
        content = novita_chat(prompt, 800, 0.3)
        json_match = content[content.index("{"):content.rindex("}")+1]
        return json.loads(json_match)
    except:
        return {"cs_email": None, "exec_emails": [], "twitter": None, "linkedin": None, "headquarters": None, "bbb_url": None, "phone": None}

def execute_actionlayer(goal, credentials=None):
    headers = {
        "Authorization": f"Bearer {ACTIONLAYER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"goal": goal}
    if credentials:
        payload["credentials"] = credentials
    try:
        response = requests.post(f"{ACTIONLAYER_BASE_URL}/tasks", headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            return {"success": True, "result": response.json()}
        else:
            return {"success": False, "error": response.text}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/complaint", methods=["POST"])
def create_complaint():
    data = request.json
    try:
        complaint_letter = generate_complaint_letter(
            data["company"], data["violation"], data["desired_outcome"],
            data.get("evidence", ""), data.get("tone", "firm")
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    contacts = find_company_contacts(data["company"])

    complaint_id = f"COMP-{int(time.time())}"
    complaints_db[complaint_id] = {
        "id": complaint_id,
        "company": data["company"],
        "violation": data["violation"],
        "desired_outcome": data["desired_outcome"],
        "letter": complaint_letter,
        "contacts": contacts,
        "status": "draft",
        "actions": [],
        "created_at": datetime.now().isoformat()
    }

    return jsonify({
        "complaint_id": complaint_id,
        "letter": complaint_letter,
        "contacts": contacts
    })

@app.route("/api/complaint/<complaint_id>/send-email", methods=["POST"])
def send_email(complaint_id):
    if complaint_id not in complaints_db:
        return jsonify({"error": "Not found"}), 404

    complaint = complaints_db[complaint_id]
    target_email = request.json.get("email", complaint["contacts"].get("cs_email"))
    custom_letter = request.json.get("letter", complaint["letter"])

    if not target_email:
        return jsonify({"error": "No email address found. Add one manually."}), 400

    goal = f"Send an email to {target_email} about {complaint['company']}. Subject: Formal Complaint - {complaint['company']}. Body: {custom_letter}"
    result = execute_actionlayer(goal)

    complaint["actions"].append({
        "type": "email",
        "target": target_email,
        "timestamp": datetime.now().isoformat(),
        "result": result
    })
    complaint["status"] = "sent"

    return jsonify(result)

@app.route("/api/complaint/<complaint_id>/send-exec", methods=["POST"])
def send_to_exec(complaint_id):
    if complaint_id not in complaints_db:
        return jsonify({"error": "Not found"}), 404

    complaint = complaints_db[complaint_id]
    exec_email = request.json.get("email")

    if not exec_email:
        exec_emails = complaint["contacts"].get("exec_emails", [])
        if exec_emails:
            exec_email = exec_emails[0]
        else:
            return jsonify({"error": "No executive email found"}), 400

    custom_letter = request.json.get("letter", complaint["letter"])
    goal = f"Send an URGENT email to executive {exec_email} at {complaint['company']}. This is escalated. Subject: URGENT: Unresolved Customer Complaint - Immediate Attention Required. Body: {custom_letter}"
    result = execute_actionlayer(goal)

    complaint["actions"].append({
        "type": "exec_email",
        "target": exec_email,
        "timestamp": datetime.now().isoformat(),
        "result": result
    })

    return jsonify(result)

@app.route("/api/complaint/<complaint_id>/tweet", methods=["POST"])
def tweet_complaint(complaint_id):
    if complaint_id not in complaints_db:
        return jsonify({"error": "Not found"}), 404

    complaint = complaints_db[complaint_id]
    custom_tweet = request.json.get("tweet")
    twitter_handle = complaint["contacts"].get("twitter", "")

    if not custom_tweet:
        prompt = f"Write a factual but compelling tweet (under 280 chars) about this complaint against {complaint['company']}: {complaint['violation']}. Be specific about what happened. Don't use hashtags excessively."
        try:
            custom_tweet = novita_chat(prompt, 300, 0.8)
        except:
            custom_tweet = f"@{twitter_handle.lstrip('@')} I had a terrible experience with {complaint['company']}. {complaint['violation'][:100]} Waiting for resolution."

    if twitter_handle and f"@{twitter_handle.lstrip('@')}" not in custom_tweet:
        custom_tweet = f"@{twitter_handle.lstrip('@')} {custom_tweet}"

    goal = f"Post this tweet on Twitter/X: {custom_tweet}"
    result = execute_actionlayer(goal)

    complaint["actions"].append({
        "type": "tweet",
        "target": twitter_handle,
        "content": custom_tweet,
        "timestamp": datetime.now().isoformat(),
        "result": result
    })

    return jsonify({"tweet": custom_tweet, "result": result})

@app.route("/api/complaint/<complaint_id>/bbb", methods=["POST"])
def file_bbb(complaint_id):
    if complaint_id not in complaints_db:
        return jsonify({"error": "Not found"}), 404

    complaint = complaints_db[complaint_id]

    prompt = f"""Write a formal BBB complaint about {complaint['company']}.

Issue: {complaint['violation']}
Desired resolution: {complaint['desired_outcome']}

BBB complaints should be:
- Factual and specific
- Include dates, names, reference numbers if available
- State what you want as resolution
- Under 300 words"""

    try:
        bbb_letter = novita_chat(prompt, 600, 0.5)
    except:
        bbb_letter = f"I am filing a complaint against {complaint['company']}. {complaint['violation']}. I am seeking {complaint['desired_outcome']}."

    goal = f"File a BBB complaint against {complaint['company']}. Go to bbb.org, search for {complaint['company']}, click 'File a Complaint', and submit this: {bbb_letter}"
    result = execute_actionlayer(goal)

    complaint["actions"].append({
        "type": "bbb",
        "target": "BBB",
        "content": bbb_letter,
        "timestamp": datetime.now().isoformat(),
        "result": result
    })

    return jsonify({"letter": bbb_letter, "result": result})

@app.route("/api/complaint/<complaint_id>/ripoffreport", methods=["POST"])
def ripoff_report(complaint_id):
    if complaint_id not in complaints_db:
        return jsonify({"error": "Not found"}), 404

    complaint = complaints_db[complaint_id]

    prompt = f"""Write a factual Ripoff Report post about {complaint['company']}.
Be specific about what happened. Include dates and details.
Title should be: "{complaint['company']} - {complaint['violation'][:50]}"
Body under 400 words. Factual, not emotional."""

    try:
        report = novita_chat(prompt, 800, 0.6)
    except:
        report = f"Subject: {complaint['company']} - {complaint['violation'][:100]}\n\n{complaint['letter'][:400]}"

    goal = f"Post on RipoffReport.com: {report}"
    result = execute_actionlayer(goal)

    complaint["actions"].append({
        "type": "ripoff_report",
        "target": "Ripoff Report",
        "content": report,
        "timestamp": datetime.now().isoformat(),
        "result": result
    })

    return jsonify({"report": report, "result": result})

@app.route("/api/complaint/<complaint_id>/pissedconsumer", methods=["POST"])
def pissed_consumer(complaint_id):
    if complaint_id not in complaints_db:
        return jsonify({"error": "Not found"}), 404

    complaint = complaints_db[complaint_id]

    prompt = f"""Write a PissedConsumer review about {complaint['company']}.
Title: Star rating 1/5 - "{complaint['violation'][:60]}"
Be factual. Include specific details. Under 400 words."""

    try:
        review = novita_chat(prompt, 800, 0.6)
    except:
        review = f"1/5 Stars - {complaint['violation']}\n\n{complaint['letter'][:400]}"

    goal = f"Post on PissedConsumer.com: {review}"
    result = execute_actionlayer(goal)

    complaint["actions"].append({
        "type": "pissed_consumer",
        "target": "Pissed Consumer",
        "content": review,
        "timestamp": datetime.now().isoformat(),
        "result": result
    })

    return jsonify({"review": review, "result": result})

@app.route("/api/complaint/<complaint_id>/twitter-escalation", methods=["POST"])
def twitter_escalation(complaint_id):
    if complaint_id not in complaints_db:
        return jsonify({"error": "Not found"}), 404

    complaint = complaints_db[complaint_id]
    target = request.json.get("target", "all")

    tweets = []
    twitter_handle = complaint["contacts"].get("twitter", "")

    if target in ["all", "company"] and twitter_handle:
        prompt = f"Angry tweet at @{twitter_handle.lstrip('@')} about: {complaint['violation']}. Under 280 chars. Tag them."
        try:
            tweet1 = novita_chat(prompt, 300, 0.8)
        except:
            tweet1 = f"@{twitter_handle.lstrip('@')} {complaint['violation'][:150]}"
        tweets.append({"to": twitter_handle, "tweet": tweet1})

    if target in ["all", "executives"]:
        exec_twitter = request.json.get("exec_twitter")
        if exec_twitter:
            prompt = f"Tweet to executive @{exec_twitter.lstrip('@')} at {complaint['company']}: {complaint['violation']}. Professional but firm. Under 280 chars."
            try:
                tweet2 = novita_chat(prompt, 300, 0.8)
            except:
                tweet2 = f"@{exec_twitter.lstrip('@')} Escalating unresolved issue with {complaint['company']}. {complaint['violation'][:100]}"
            tweets.append({"to": exec_twitter, "tweet": tweet2})

    results = []
    for t in tweets:
        goal = f"Post this tweet: {t['tweet']}"
        result = execute_actionlayer(goal)
        results.append({"handle": t["to"], "tweet": t["tweet"], "result": result})

        complaint["actions"].append({
            "type": "tweet",
            "target": t["to"],
            "content": t["tweet"],
            "timestamp": datetime.now().isoformat(),
            "result": result
        })

    return jsonify({"tweets": results})

@app.route("/api/complaint/<complaint_id>/follow-up", methods=["POST"])
def follow_up(complaint_id):
    if complaint_id not in complaints_db:
        return jsonify({"error": "Not found"}), 404

    complaint = complaints_db[complaint_id]
    attempt_num = len([a for a in complaint["actions"] if a["type"] == "follow_up"]) + 1

    prompt = f"""Write follow-up complaint #{attempt_num} to {complaint['company']}.
Original complaint: {complaint['letter'][:500]}
This is attempt #{attempt_num}. Escalate the tone.
Mention BBB, Attorney General, social media.
Under 200 words."""

    try:
        follow_up_letter = novita_chat(prompt, 600, 0.7)
    except:
        follow_up_letter = f"Following up on my complaint to {complaint['company']}. This is attempt #{attempt_num}. If not resolved, I will escalate to BBB and social media."

    target_email = complaint["contacts"].get("cs_email")
    if target_email:
        goal = f"Send follow-up email to {target_email}: {follow_up_letter}"
        result = execute_actionlayer(goal)
    else:
        result = {"success": False, "error": "No email configured"}

    complaint["actions"].append({
        "type": "follow_up",
        "target": target_email,
        "content": follow_up_letter,
        "timestamp": datetime.now().isoformat(),
        "result": result
    })

    return jsonify({"letter": follow_up_letter, "result": result})

@app.route("/api/complaint/<complaint_id>", methods=["GET"])
def get_complaint(complaint_id):
    if complaint_id not in complaints_db:
        return jsonify({"error": "Not found"}), 404
    return jsonify(complaints_db[complaint_id])

@app.route("/api/complaints", methods=["GET"])
def list_complaints():
    return jsonify(list(complaints_db.values()))

if __name__ == "__main__":
    app.run(debug=True, port=5000)
