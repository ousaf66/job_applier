"""
Shared core for the Vercel deployment.

Serverless changes three things about how this tool has to work:

  1. There is no disk. jobs.csv / sent_log.csv / config.json are gone; everything
     lives in Redis (Upstash, added through the Vercel dashboard) and is reached
     over its REST API, so there is no database driver to install.
  2. There is no long-running process. A function that slept 45 seconds between
     emails would be killed. So /api/send sends exactly ONE email per request and
     the browser paces the batch.
  3. There are no secrets on disk. The Gmail app password and the login password
     come from Vercel environment variables.

Standard library only — no requirements.txt needed.
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ASSETS = Path(__file__).resolve().parent / "assets"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
JOB_FIELDS = ["company", "role", "contact_name", "email", "source", "job_url",
              "location", "notes", "status", "sent_at"]
CONFIG_FIELDS = ["sender_name", "reply_to", "resume_filename", "phone",
                 "linkedin", "github", "portfolio"]
PARKED = {"skip", "hold", "no", "failed"}
SESSION_TTL = 12 * 3600


class Abort(Exception):
    """A problem worth showing the user verbatim."""


# --------------------------------------------------------------------------- #
# storage — Upstash Redis over REST
# --------------------------------------------------------------------------- #

def _kv_credentials():
    url = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
    token = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    return (url or "").rstrip("/"), token or ""


def storage_ready():
    url, token = _kv_credentials()
    return bool(url and token)


def kv(*command):
    url, token = _kv_credentials()
    if not url or not token:
        raise Abort("No storage connected. In the Vercel dashboard open Storage, create an "
                    "Upstash Redis database and connect it to this project, then redeploy.")
    request = urllib.request.Request(
        url,
        data=json.dumps([str(c) for c in command]).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise Abort("Storage rejected the request (HTTP %d). Check the Upstash integration." % exc.code)
    except OSError as exc:
        raise Abort("Could not reach storage: %s" % exc)
    if isinstance(body, dict) and body.get("error"):
        raise Abort("Storage error: %s" % body["error"])
    return body.get("result") if isinstance(body, dict) else None


def kv_get_json(key, default=None):
    raw = kv("GET", key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def kv_set_json(key, value):
    kv("SET", key, json.dumps(value))


# --------------------------------------------------------------------------- #
# the queue, templates, config, resume
# --------------------------------------------------------------------------- #

def seed_jobs():
    path = ASSETS / "jobs.seed.csv"
    if not path.exists():
        return []
    rows = []
    for row in csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))):
        rows.append({k: (row.get(k) or "") for k in JOB_FIELDS})
    return rows


def get_jobs():
    rows = kv_get_json("jobs")
    if rows is None:                       # first ever request — seed from the repo
        rows = seed_jobs()
        kv_set_json("jobs", rows)
    return rows


def put_jobs(rows):
    clean = []
    for row in rows:
        item = {k: str(row.get(k) or "").strip() for k in JOB_FIELDS}
        if any(item.values()):
            clean.append(item)
    kv_set_json("jobs", clean)
    return len(clean)


def get_template_names():
    names = kv_get_json("template_names")
    if names is None:
        names = []
        for path in sorted((ASSETS / "templates").glob("*.txt")):
            names.append(path.name)
            kv("SET", "template:" + path.name, path.read_text(encoding="utf-8"))
        kv_set_json("template_names", names)
    return names


def get_template_text(name):
    get_template_names()
    raw = kv("GET", "template:" + name)
    if raw is None:
        raise Abort("No such template: %s" % name)
    return raw


def put_template_text(name, text):
    if name not in get_template_names():
        raise Abort("No such template: %s" % name)
    kv("SET", "template:" + name, text)


def split_template(raw):
    if not raw.lower().startswith("subject:"):
        raise Abort("The template must start with a 'Subject: ...' line.")
    subject_line, _, body = raw.partition("\n")
    return subject_line.split(":", 1)[1].strip(), body.lstrip("\n")


def get_config():
    stored = kv_get_json("config") or {}
    cfg = {k: str(stored.get(k) or "") for k in CONFIG_FIELDS}
    cfg["sender_email"] = os.environ.get("GMAIL_ADDRESS", "").strip()
    return cfg


def put_config(patch):
    stored = kv_get_json("config") or {}
    for key in CONFIG_FIELDS:
        if key in patch:
            stored[key] = str(patch[key]).strip()
    kv_set_json("config", stored)


def get_resume():
    encoded = kv("GET", "resume_b64")
    if not encoded:
        raise Abort("No resume uploaded yet. Add your PDF on the Setup tab.")
    name = kv("GET", "resume_name") or "resume.pdf"
    return base64.b64decode(encoded), name


def put_resume(encoded, name):
    if "," in encoded[:120] and encoded.strip().startswith("data:"):
        encoded = encoded.split(",", 1)[1]          # strip a data: URL prefix
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        raise Abort("That file could not be read.")
    if len(raw) > 4 * 1024 * 1024:
        raise Abort("Resume is larger than 4 MB.")
    if not raw.startswith(b"%PDF"):
        raise Abort("That is not a PDF file.")
    kv("SET", "resume_b64", base64.b64encode(raw).decode("ascii"))
    kv("SET", "resume_name", name or "resume.pdf")
    return len(raw)


def resume_info():
    name = kv("GET", "resume_name")
    if not name:
        return {"ok": False, "name": "", "bytes": 0}
    encoded = kv("GET", "resume_b64") or ""
    return {"ok": True, "name": name, "bytes": (len(encoded) * 3) // 4}


# --------------------------------------------------------------------------- #
# the sent log
# --------------------------------------------------------------------------- #

def get_log():
    return kv_get_json("sent_log", []) or []


def append_log(entry):
    entries = get_log()
    entries.append(entry)
    kv_set_json("sent_log", entries[-500:])


def sent_pairs():
    seen = set()
    for entry in get_log():
        if entry.get("result") == "sent":
            seen.add((str(entry.get("email", "")).lower().strip(),
                      str(entry.get("company", "")).lower().strip()))
    return seen


# --------------------------------------------------------------------------- #
# rendering — same rules as the original send_applications.py
# --------------------------------------------------------------------------- #

def render(text, row, cfg):
    contact = (row.get("contact_name") or "").strip()
    values = {
        "company": (row.get("company") or "").strip(),
        "role": (row.get("role") or "the role").strip(),
        "contact_name": contact,
        "greeting_name": contact if contact else "Hiring Team",
        "location": (row.get("location") or "").strip(),
        "source": (row.get("source") or "").strip(),
        "job_url": (row.get("job_url") or "").strip(),
        "notes": (row.get("notes") or "").strip(),
        "sender_name": cfg.get("sender_name", ""),
        "sender_email": cfg.get("sender_email", ""),
        "phone": cfg.get("phone", ""),
        "linkedin": cfg.get("linkedin", ""),
        "github": cfg.get("github", ""),
        "portfolio": cfg.get("portfolio", ""),
    }
    out = text
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value)
    kept = [ln for ln in out.split("\n") if not re.match(r"^\s*(Role link:|Source:)\s*$", ln)]
    leftover = re.findall(r"\{\{(\w+)\}\}", "\n".join(kept))
    if leftover:
        raise Abort("The template has unknown placeholders: %s" % ", ".join(sorted(set(leftover))))
    return "\n".join(kept)


def build_message(row, cfg, subject_tpl, body_tpl, to_addr=None):
    if not cfg.get("sender_email"):
        raise Abort("GMAIL_ADDRESS is not set in your Vercel environment variables.")
    resume_bytes, resume_name = get_resume()
    msg = EmailMessage()
    msg["From"] = "%s <%s>" % (cfg.get("sender_name") or cfg["sender_email"], cfg["sender_email"])
    msg["To"] = to_addr or (row.get("email") or "").strip()
    msg["Subject"] = render(subject_tpl, row, cfg)
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    msg.set_content(render(body_tpl, row, cfg))
    msg.add_attachment(
        resume_bytes, maintype="application", subtype="pdf",
        filename=cfg.get("resume_filename") or resume_name,
    )
    return msg


def check(row, seen):
    """Reason this row must be skipped, or None."""
    status = (row.get("status") or "").strip().lower()
    if status == "sent":
        return "already marked sent"
    if status in PARKED:
        return "status=%s" % status
    if not (row.get("company") or "").strip():
        return "no company"
    email = (row.get("email") or "").strip()
    if not email:
        return "no email address"
    if not EMAIL_RE.match(email):
        return "malformed email (%s)" % email
    if (email.lower(), (row.get("company") or "").lower().strip()) in seen:
        return "already in the sent log"
    return None


def eligible(rows, only=""):
    seen = sent_pairs()
    out = []
    for index, row in enumerate(rows):
        if only and only.lower() not in (row.get("company") or "").lower():
            continue
        if check(row, seen) is None:
            out.append(index)
    return out


def local_stamp(tz_offset_minutes=0):
    try:
        offset = int(tz_offset_minutes)
    except (TypeError, ValueError):
        offset = 0
    offset = max(-840, min(840, offset))
    return (datetime.utcnow() + timedelta(minutes=offset)).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# sending
# --------------------------------------------------------------------------- #

def smtp_send(msg, cfg):
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not password:
        raise Abort("GMAIL_APP_PASSWORD is not set in your Vercel environment variables.")
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=25) as server:
            server.login(cfg["sender_email"], password.replace(" ", ""))
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise Abort("Gmail rejected the login. GMAIL_APP_PASSWORD must be a 16-character Google "
                    "App Password belonging to GMAIL_ADDRESS, with 2-Step Verification on.")
    except (OSError, smtplib.SMTPException) as exc:
        raise Abort("Could not send through Gmail SMTP from Vercel (%s). If this keeps happening "
                    "the platform is blocking the connection — set RESEND_API_KEY and "
                    "RESEND_FROM to send over HTTPS instead." % exc)


def resend_send(msg, cfg):
    """Fallback for when outbound SMTP is blocked. Needs a domain you have verified
    with Resend — Gmail addresses cannot be used as the From."""
    key = os.environ.get("RESEND_API_KEY", "")
    sender = os.environ.get("RESEND_FROM", "")
    attachment = None
    for part in msg.iter_attachments():
        attachment = {"filename": part.get_filename() or "resume.pdf",
                      "content": base64.b64encode(part.get_payload(decode=True)).decode("ascii")}
        break
    payload = {
        "from": sender,
        "to": [msg["To"]],
        "subject": msg["Subject"],
        "text": msg.get_body(preferencelist=("plain",)).get_content(),
        "reply_to": cfg.get("reply_to") or cfg["sender_email"],
    }
    if attachment:
        payload["attachments"] = [attachment]
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        raise Abort("Resend refused the email: %s" % exc.read().decode("utf-8", "replace")[:300])
    except OSError as exc:
        raise Abort("Could not reach Resend: %s" % exc)


def deliver(msg, cfg):
    if os.environ.get("RESEND_API_KEY") and os.environ.get("RESEND_FROM"):
        return resend_send(msg, cfg)
    return smtp_send(msg, cfg)


# --------------------------------------------------------------------------- #
# login — stateless signed tokens, because there is no server memory
# --------------------------------------------------------------------------- #

def ui_password():
    return os.environ.get("UI_PASSWORD", "")


def _secret():
    return (os.environ.get("SESSION_SECRET") or ui_password() or "unset").encode("utf-8")


def make_token(ttl=SESSION_TTL):
    expiry = str(int(time.time() + ttl))
    signature = hmac.new(_secret(), expiry.encode("ascii"), hashlib.sha256).hexdigest()[:40]
    return expiry + "." + signature


def valid_token(token):
    try:
        expiry, signature = str(token).split(".", 1)
        if int(expiry) < time.time():
            return False
    except (ValueError, AttributeError):
        return False
    expected = hmac.new(_secret(), expiry.encode("ascii"), hashlib.sha256).hexdigest()[:40]
    return hmac.compare_digest(expected, signature)


def check_login(password):
    stored = ui_password()
    if not stored:
        raise Abort("UI_PASSWORD is not set in your Vercel environment variables. "
                    "Without it anyone could open this page and send email as you.")
    if not hmac.compare_digest(stored, str(password or "")):
        return None
    return make_token()


# --------------------------------------------------------------------------- #
# what the page loads on every refresh
# --------------------------------------------------------------------------- #

def state_payload():
    env = {
        "gmail_address": os.environ.get("GMAIL_ADDRESS", ""),
        "has_app_password": bool(os.environ.get("GMAIL_APP_PASSWORD")),
        "has_ui_password": bool(ui_password()),
        "storage": storage_ready(),
        "transport": "resend" if (os.environ.get("RESEND_API_KEY")
                                  and os.environ.get("RESEND_FROM")) else "gmail-smtp",
    }
    if not env["storage"]:
        return {"env": env, "jobs": [], "counts": {"total": 0, "ready": 0, "blocked": 0, "sent": 0},
                "templates": [], "config": {}, "sent_log": [], "resume": {"ok": False},
                "fatal": "No storage connected."}
    rows = get_jobs()
    seen = sent_pairs()
    jobs = []
    for row in rows:
        item = dict(row)
        item["_skip"] = check(row, seen) or ""
        jobs.append(item)
    ready = sum(1 for j in jobs if not j["_skip"])
    sent = sum(1 for j in jobs if (j.get("status") or "").lower() == "sent")
    missing = []
    if not env["gmail_address"]:
        missing.append("GMAIL_ADDRESS")
    if not env["has_app_password"] and env["transport"] == "gmail-smtp":
        missing.append("GMAIL_APP_PASSWORD")
    resume = resume_info()
    if not resume["ok"]:
        missing.append("resume")
    return {
        "env": env,
        "config": get_config(),
        "jobs": jobs,
        "counts": {"total": len(jobs), "ready": ready, "blocked": len(jobs) - ready, "sent": sent},
        "templates": get_template_names(),
        "sent_log": get_log()[-100:],
        "resume": resume,
        "missing": missing,
        "ready": not missing,
        "defaults": {"limit": 25, "delay": 45},
    }


# --------------------------------------------------------------------------- #
# the shape every endpoint file uses
# --------------------------------------------------------------------------- #

def endpoint(fn, methods=("POST",), auth=True):
    class Endpoint(BaseHTTPRequestHandler):
        def _reply(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self):
            if self.command not in methods:
                return self._reply({"error": "method not allowed"}, 405)
            if auth and not valid_token(self.headers.get("X-Token") or ""):
                return self._reply({"error": "not logged in"}, 403)
            try:
                if self.command == "GET":
                    query = parse_qs(urlparse(self.path).query)
                    payload = {k: v[0] for k, v in query.items()}
                else:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                self._reply(fn(payload))
            except Abort as exc:
                self._reply({"error": str(exc)}, 400)
            except Exception as exc:                  # noqa: BLE001 - surfaced in the UI
                self._reply({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

        do_GET = _dispatch
        do_POST = _dispatch

        def log_message(self, fmt, *args):
            pass

    return Endpoint
