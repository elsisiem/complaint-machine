# Complaint Machine

An AI agent that files complaints for you and escalates until resolved.

## What It Does

1. **You describe the problem** - Company name, what happened, what you want
2. **AI generates a complaint letter** - Legally-informed, references CCPA/FTC, matches your chosen tone
3. **Discovers company contacts** - CS emails, executive contacts, social handles, phone numbers
4. **Sends through multiple channels** - Email, Twitter/X, contact forms
5. **Escalates automatically** - BBB complaints, Ripoff Report, PissedConsumer, mass tweeting executives

## How It Works

| Component | Role |
|-----------|------|
| **Novita AI** | Generates complaint letters, discovers contacts, creates escalation content |
| **ActionLayer** | Executes real actions - sends emails, posts to websites, files complaints |

## Escalation Ladder

```
Level 1: Email customer service
Level 2: Email executives directly
Level 3: Tweet at company publicly
Level 4: File BBB complaint
Level 5: Post on Ripoff Report
Level 6: Post on PissedConsumer
Level 7: Mass tweet at company + all executives
```

## Setup

### 1. Get API Keys

- **Novita AI**: Sign up at [novita.ai](https://novita.ai) and add credits
- **ActionLayer**: Sign up at [actionlayer.io](https://actionlayer.io) and connect your email/Twitter

### 2. Install

```bash
git clone https://github.com/elsisiem/complaint-machine.git
cd complaint-machine
pip install -r requirements.txt
```

### 3. Configure

Create a `.env` file:

```
NOVITA_API_KEY=your_novita_key
ACTIONLAYER_API_KEY=your_actionlayer_key
```

### 4. Run

```bash
python app.py
```

Open http://localhost:5000

## Usage

1. Fill in the complaint form (company, what happened, what you want)
2. Click **Generate Complaint** - AI writes a professional letter
3. Review the letter and discovered contacts
4. Click **Send** next to any channel (email, exec, twitter)
5. Use the **Escalation Ladder** to file BBB complaints, post publicly, or mass-tweet

## Tech Stack

- **Backend**: Python, Flask
- **Frontend**: Tailwind CSS, Vanilla JS
- **AI**: Novita AI (Llama 3.3 70B)
- **Execution**: ActionLayer API

## License

MIT
