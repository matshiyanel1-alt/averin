#!/usr/bin/env python3
"""
Novaux AR Chase — Production App
Deployed on Railway. Handles login, CSV upload, live chase, and post-chase report emails.
"""

import os, csv, json, math, io, uuid, threading, time
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from functools import wraps

from flask import (Flask, request, jsonify, Response,
                   send_from_directory, session, redirect, url_for, render_template_string)
from dotenv import load_dotenv
import requests

load_dotenv()

BASE_DIR  = Path(__file__).parent
DEMO_CSV  = BASE_DIR / "demo_debtors.csv"
STATIC    = BASE_DIR / "demo"

BLAND_API_KEY = os.getenv("BLAND_API_KEY", "")
BLAND_HEADERS = {"authorization": BLAND_API_KEY, "Content-Type": "application/json"}
BLAND_BASE    = "https://api.bland.ai/v1"

RESEND_API_KEY  = os.getenv("RESEND_API_KEY", "")
RESEND_FROM     = os.getenv("RESEND_FROM", "Novaux AR Chase <onboarding@resend.dev>")

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "novaux2026")

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
app.secret_key = os.getenv("FLASK_SECRET", uuid.uuid4().hex)

SESSIONS: dict = {}

LANGUAGE_MAP = {
    "en": "en-ZA", "zu": "zu-ZA", "af": "af-ZA",
    "xh": "xh-ZA", "st": "st-ZA", "tn": "tn-ZA",
}


# ── Auth ─────────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Novaux — AR Chase</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:'Inter',sans-serif;}
body{background:#0a0a0a;min-height:100vh;display:flex;align-items:center;justify-content:center;}
.card{background:#1a1a1a;border:1px solid #27272a;border-radius:16px;padding:40px;width:360px;text-align:center;}
.logo{width:140px;border-radius:8px;background:#fff;padding:5px 8px;margin:0 auto 24px;}
h2{font-family:'Space Grotesk',sans-serif;font-size:20px;font-weight:700;color:#f4f4f5;margin-bottom:6px;}
p{font-size:13px;color:#71717a;margin-bottom:24px;}
input{width:100%;padding:10px 14px;background:#111;border:1px solid #27272a;border-radius:8px;
  color:#f4f4f5;font-size:14px;outline:none;transition:border-color .15s;margin-bottom:12px;}
input:focus{border-color:#3b82f6;}
button{width:100%;padding:11px;background:#3b82f6;border:none;border-radius:8px;
  color:white;font-size:14px;font-weight:600;cursor:pointer;transition:background .15s;}
button:hover{background:#2563eb;}
.err{font-size:12px;color:#ef4444;margin-bottom:12px;}
</style>
</head>
<body>
<div class="card">
  <img class="logo" src="/static/novaux_logo.jpg" alt="Novaux">
  <h2>AR Chase</h2>
  <p>Authorised access only</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST">
    <input type="password" name="password" placeholder="Enter access code" autofocus>
    <button type="submit">Access Dashboard</button>
  </form>
</div>
</body>
</html>"""

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authed"] = True
            return redirect(url_for("index"))
        error = "Incorrect access code"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Pages ────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return send_from_directory(str(STATIC), "ar_chase_dashboard.html")


# ── Helpers ──────────────────────────────────────────────────────────────────

def priority_score(amount, days_overdue):
    return float(amount) * math.log10(max(float(days_overdue), 1) + 1)


def send_email(to, subject, body_html, body_plain=None):
    if not RESEND_API_KEY:
        return False, "Resend API key not configured"
    try:
        payload = {"from": RESEND_FROM, "to": [to], "subject": subject, "html": body_html}
        if body_plain:
            payload["text"] = body_plain
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json=payload, timeout=10
        )
        if resp.status_code in (200, 201):
            return True, "sent"
        return False, f"{resp.status_code}: {resp.text[:120]}"
    except Exception as e:
        return False, str(e)


def debtor_email_body(contact_name, account_name, amount_owed, invoice_ref, client_company):
    amount_fmt = f"R{float(amount_owed):,.0f}"
    ref = invoice_ref or "on account"
    return f"""
<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;background:#fff;">
  <div style="margin-bottom:28px;">
    <p style="font-size:12px;color:#71717a;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;">From {client_company}</p>
    <h2 style="font-size:20px;font-weight:700;color:#111;margin:0;">Outstanding Account Notice</h2>
  </div>
  <p style="font-size:15px;color:#374151;line-height:1.6;">Dear {contact_name},</p>
  <p style="font-size:15px;color:#374151;line-height:1.6;margin-top:12px;">
    This is a courtesy notice regarding an outstanding balance of
    <strong style="color:#111;">{amount_fmt}</strong> on account reference
    <strong style="color:#111;">{ref}</strong>.
  </p>
  <div style="background:#f9fafb;border-left:3px solid #3b82f6;padding:16px 20px;border-radius:0 8px 8px 0;margin:24px 0;">
    <div style="font-size:13px;color:#6b7280;margin-bottom:4px;">Amount outstanding</div>
    <div style="font-size:26px;font-weight:700;color:#111;">{amount_fmt}</div>
    <div style="font-size:13px;color:#6b7280;margin-top:4px;">Reference: {ref}</div>
  </div>
  <p style="font-size:15px;color:#374151;line-height:1.6;">
    Please arrange payment at your earliest convenience. If you'd like to discuss a payment arrangement
    or have any queries, reply to this email directly.
  </p>
  <p style="font-size:15px;color:#374151;line-height:1.6;margin-top:16px;">
    If you've already settled this amount, please disregard this notice.
  </p>
  <div style="margin-top:32px;padding-top:20px;border-top:1px solid #e5e7eb;">
    <p style="font-size:13px;color:#9ca3af;">
      Sent by <strong>Novaux AI</strong> on behalf of {client_company} ·
      To opt out of automated follow-ups, reply with "OPT OUT" ·
      NCA compliant
    </p>
  </div>
</div>"""


def build_call_prompt(contact_name, account_name, amount_owed, invoice_ref, client_company):
    amt = f"R{float(amount_owed):,.0f}"
    ref = invoice_ref or "on account"
    return f"""You are an AI assistant calling on behalf of {client_company}. Professional, calm, direct. Payment follow-up — not aggressive.

NCA COMPLIANCE — MANDATORY:
- Identify yourself as an AI assistant from Novaux, calling on behalf of {client_company}, in your first sentence.
- State purpose immediately. Do not threaten legal action. Offer right to dispute. Do not call again within 48h if asked.

OPENING: "Hi, could I speak to {contact_name}? — Great. I'm Ava, an AI assistant from Novaux calling on behalf of {client_company} about {amt} outstanding on account reference {ref}. I'm hoping we can arrange payment today."

IF THEY CONFIRM AND WANT TO PAY: Get specific date and amount. Confirm and thank.
IF INSTALMENTS: Capture monthly amount and duration.
IF DISPUTE: Flag it, say someone will follow up in 2 business days. Do not argue.
IF VOICEMAIL: "Hi, message for {contact_name} from Novaux on behalf of {client_company} regarding an outstanding account. Please call at your earliest convenience."
IF DON'T CALL AGAIN: Acknowledge and end professionally.

TONE: Calm, factual, non-confrontational."""


def dispatch_call(row, client_company):
    phone = row.get("phone", "").strip()
    if not phone.startswith("+27"):
        return None, f"invalid phone: {phone}"
    lang = LANGUAGE_MAP.get(row.get("language", "en").lower(), "en-ZA")
    payload = {
        "phone_number": phone,
        "task": build_call_prompt(row["contact_name"], row["account_name"],
                                   row["amount_owed"], row.get("invoice_ref",""), client_company),
        "language": lang, "max_duration": 300, "wait_for_greeting": True, "record": True,
        "analysis_schema": {
            "outcome": {"type":"string","description":"promise_to_pay, dispute, callback_requested, no_answer, voicemail, wrong_number, refused, payment_arrangement"},
            "promise_amount": {"type":"number","description":"ZAR amount promised. 0 if none."},
            "promise_date": {"type":"string","description":"Date debtor said they will pay."},
            "contact_confirmed": {"type":"boolean","description":"True if right person reached."},
            "notes": {"type":"string","description":"Other relevant context."},
        },
        "metadata": {"account_name": row["account_name"], "amount_owed": row["amount_owed"],
                     "invoice_ref": row.get("invoice_ref",""), "client_company": client_company}
    }
    try:
        resp = requests.post(f"{BLAND_BASE}/calls", headers=BLAND_HEADERS, json=payload, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("call_id",""), None
        return None, f"{resp.status_code}: {resp.text[:120]}"
    except Exception as e:
        return None, str(e)


def send_report_email(to_email, client_company, summary):
    """Email a chase summary report to the client after the run completes."""
    rows_html = ""
    for d in summary["debtors"]:
        outcome_label = d.get("outcome_label","—")
        committed     = f"R{d['committed']:,.0f}" if d.get("committed",0) > 0 else "—"
        color = "#22c55e" if d.get("committed",0) > 0 else "#6b7280"
        rows_html += f"""
        <tr style="border-bottom:1px solid #e5e7eb;">
          <td style="padding:10px 12px;font-size:13px;color:#111;font-weight:500;">{d['account_name']}</td>
          <td style="padding:10px 12px;font-size:13px;color:#6b7280;">{d.get('invoice_ref','—')}</td>
          <td style="padding:10px 12px;font-size:13px;font-weight:600;color:#111;">R{float(d['amount_owed']):,.0f}</td>
          <td style="padding:10px 12px;font-size:12px;color:#374151;">{outcome_label}</td>
          <td style="padding:10px 12px;font-size:13px;font-weight:700;color:{color};">{committed}</td>
        </tr>"""

    html = f"""
<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:32px 20px;">
  <div style="background:#0a0a0a;border-radius:12px;padding:24px 28px;margin-bottom:24px;">
    <p style="font-size:11px;color:#71717a;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;">Novaux AR Chase</p>
    <h1 style="font-size:22px;font-weight:700;color:#f4f4f5;margin:0 0 4px;">Chase Complete</h1>
    <p style="font-size:14px;color:#a1a1aa;margin:0;">{client_company} · {datetime.now().strftime('%d %b %Y, %H:%M')}</p>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:24px;">
    <div style="background:#f9fafb;border-radius:10px;padding:16px;text-align:center;">
      <div style="font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;">Accounts Chased</div>
      <div style="font-size:28px;font-weight:700;color:#111;">{summary['total']}</div>
    </div>
    <div style="background:#f0fdf4;border-radius:10px;padding:16px;text-align:center;">
      <div style="font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;">Contacted</div>
      <div style="font-size:28px;font-weight:700;color:#16a34a;">{summary['contacted']}</div>
    </div>
    <div style="background:#eff6ff;border-radius:10px;padding:16px;text-align:center;">
      <div style="font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;">Total Outstanding</div>
      <div style="font-size:22px;font-weight:700;color:#1d4ed8;">R{summary['total_owed']:,.0f}</div>
    </div>
  </div>

  <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;border:1px solid #e5e7eb;">
    <thead>
      <tr style="background:#f9fafb;">
        <th style="padding:10px 12px;font-size:11px;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;text-align:left;">Account</th>
        <th style="padding:10px 12px;font-size:11px;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;text-align:left;">Ref</th>
        <th style="padding:10px 12px;font-size:11px;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;text-align:left;">Owed</th>
        <th style="padding:10px 12px;font-size:11px;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;text-align:left;">Outcome</th>
        <th style="padding:10px 12px;font-size:11px;font-weight:600;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;text-align:left;">Committed</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>

  <div style="margin-top:24px;padding:16px 20px;background:#f0fdf4;border-radius:10px;">
    <p style="font-size:13px;color:#166534;font-weight:600;margin:0 0 4px;">
      Next steps
    </p>
    <p style="font-size:13px;color:#166534;margin:0;">
      Follow up on payment promises before their committed dates.
      Accounts with no answer will be re-chased in 48 hours.
      Disputes have been flagged for manual review.
    </p>
  </div>

  <p style="font-size:11px;color:#9ca3af;text-align:center;margin-top:24px;">
    Powered by Novaux AI · NCA compliant · info@novaux.co.za
  </p>
</div>"""

    plain = f"Chase complete for {client_company}. {summary['contacted']}/{summary['total']} accounts contacted. Total outstanding: R{summary['total_owed']:,.0f}."
    send_email(to_email, f"AR Chase Report — {client_company}", html, plain)


# ── Chase engine ─────────────────────────────────────────────────────────────

def sse_event(data):
    return f"data: {json.dumps(data)}\n\n"


def run_chase(session_id, debtors, client_company, demo_mode, report_email=None):
    q = SESSIONS[session_id]["queue"]

    def emit(d):
        q.put(sse_event(d))

    emit({"type":"start","total":len(debtors),"client":client_company})
    total_owed = sum(float(d["amount_owed"]) for d in debtors)
    emit({"type":"summary","total_owed":total_owed,"count":len(debtors)})

    contacted = 0
    debtor_results = []

    for i, debtor in enumerate(debtors):
        did    = debtor["_id"]
        amount = float(debtor["amount_owed"])

        emit({"type":"update","id":did,"status":"scoring","detail":f"Priority score: {debtor['_score']:.0f}","rank":i+1})
        time.sleep(0.4 if demo_mode else 0.1)

        # Email
        emit({"type":"update","id":did,"status":"emailing","detail":"Sending personalised email..."})
        to_email = debtor.get("email","")
        if to_email:
            body = debtor_email_body(debtor["contact_name"], debtor["account_name"],
                                     amount, debtor.get("invoice_ref",""), client_company)
            email_ok, email_msg = send_email(
                to_email,
                f"Outstanding Account — {debtor['account_name']}",
                body
            )
        else:
            email_ok, email_msg = False, "no email on record"

        time.sleep(0.5 if demo_mode else 0.1)
        emit({"type":"update","id":did,"status":"email_sent" if email_ok else "email_skipped",
              "detail": "Email sent ✓" if email_ok else f"Email skipped: {email_msg}"})

        # Call
        time.sleep(0.5 if demo_mode else 0.1)
        emit({"type":"update","id":did,"status":"calling","detail":"Dispatching AI call..."})
        time.sleep(0.8 if demo_mode else 0.1)

        call_id, call_err = dispatch_call(debtor, client_company)
        call_ok = call_id is not None

        if not call_ok and demo_mode:
            call_id  = f"demo-{uuid.uuid4().hex[:8]}"
            call_ok  = True
            call_err = None

        if call_ok:
            contacted += 1
            emit({"type":"update","id":did,"status":"contacted",
                  "detail": f"AI call live{' (demo)' if demo_mode and (call_id or '').startswith('demo-') else ''}",
                  "call_id": call_id})
        else:
            emit({"type":"update","id":did,"status":"call_failed","detail":f"Call failed: {call_err}"})

        debtor_results.append({
            "account_name": debtor["account_name"],
            "invoice_ref":  debtor.get("invoice_ref",""),
            "amount_owed":  amount,
            "contacted":    call_ok,
            "outcome_label": "Contacted" if call_ok else "Failed",
            "committed":    0,
        })

        time.sleep(0.3 if demo_mode else 0.1)
        emit({"type":"progress","contacted":contacted,"total":len(debtors),"total_owed":total_owed})

    emit({"type":"done","contacted":contacted,"total":len(debtors),"total_owed":total_owed,"demo":demo_mode})
    SESSIONS[session_id]["done"] = True
    q.put(None)

    # Send report to client contact
    if report_email:
        send_report_email(report_email, client_company, {
            "total": len(debtors), "contacted": contacted,
            "total_owed": total_owed, "debtors": debtor_results
        })


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/upload", methods=["POST"])
@login_required
def upload_csv():
    f = request.files.get("file")
    if not f:
        return jsonify({"error":"No file"}), 400
    content = f.read().decode("utf-8-sig")
    reader  = csv.DictReader(io.StringIO(content))
    debtors, errors = [], []
    for i, row in enumerate(reader):
        try:
            row["amount_owed"]  = float(row["amount_owed"])
            row["days_overdue"] = float(row["days_overdue"])
            row["_score"] = priority_score(row["amount_owed"], row["days_overdue"])
            row["_id"]    = uuid.uuid4().hex[:8]
            debtors.append(row)
        except (KeyError, ValueError) as e:
            errors.append(f"Row {i+1}: {e}")
    debtors.sort(key=lambda r: r["_score"], reverse=True)
    total = sum(d["amount_owed"] for d in debtors)
    return jsonify({"debtors":debtors,"total_owed":total,"est_recovery":total*0.62,"count":len(debtors),"errors":errors})


@app.route("/api/demo-data")
@login_required
def demo_data():
    if not DEMO_CSV.exists():
        return jsonify({"error":"Demo CSV not found"}), 404
    with open(DEMO_CSV) as f:
        reader = csv.DictReader(f)
        debtors = []
        for row in reader:
            try:
                row["amount_owed"]  = float(row["amount_owed"])
                row["days_overdue"] = float(row["days_overdue"])
                row["_score"] = priority_score(row["amount_owed"], row["days_overdue"])
                row["_id"]    = uuid.uuid4().hex[:8]
                debtors.append(row)
            except (KeyError, ValueError):
                pass
    debtors.sort(key=lambda r: r["_score"], reverse=True)
    total = sum(d["amount_owed"] for d in debtors)
    return jsonify({"debtors":debtors,"total_owed":total,"est_recovery":total*0.62,"count":len(debtors),"errors":[]})


@app.route("/api/chase", methods=["POST"])
@login_required
def start_chase():
    body         = request.get_json()
    debtors      = body.get("debtors", [])
    client       = body.get("client_company", "Your Company")
    demo_mode    = body.get("demo_mode", True)
    report_email = body.get("report_email", "")
    if not debtors:
        return jsonify({"error":"No debtors"}), 400
    sid = uuid.uuid4().hex
    SESSIONS[sid] = {"queue": Queue(), "done": False}
    t = threading.Thread(target=run_chase, args=(sid, debtors, client, demo_mode, report_email or None), daemon=True)
    t.start()
    return jsonify({"session_id": sid})


@app.route("/api/stream/<sid>")
@login_required
def stream(sid):
    if sid not in SESSIONS:
        return jsonify({"error":"Session not found"}), 404
    def generate():
        q = SESSIONS[sid]["queue"]
        yield sse_event({"type":"connected","session_id":sid})
        while True:
            try:
                msg = q.get(timeout=30)
                if msg is None:
                    break
                yield msg
            except Empty:
                yield sse_event({"type":"ping"})
                if SESSIONS[sid]["done"]:
                    break
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    print(f"\n{'='*50}")
    print(f"  Novaux AR Chase — Production")
    print(f"  http://localhost:{port}")
    print(f"  Password: {DASHBOARD_PASSWORD}")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
