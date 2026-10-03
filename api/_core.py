"""
Core of the Vercel deployment — the same app as the local web UI, without a disk.

Serverless changes three things:

  1. There is no disk. The queue, emails, cover letters, resumes, settings and sent log
     live in Redis (Upstash, added through the Vercel dashboard) and are reached over its
     REST API, so there is no database driver to install.
  2. There is no long-running process. A function that slept 45 seconds between emails
     would be killed, so /api/send sends exactly ONE email per request and the browser
     paces the batch. Keep the tab open while sending.
  3. There are no secrets on disk. The Gmail app password and the login password come
     from Vercel environment variables.

Standard library only — no requirements.txt needed. The email rendering is shared with
the local version (see _mailcore.py).
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
from pathlib import Path

import _cover_pdf as cover_pdf
from _mailcore import (PLACEHOLDERS, check, cover_filename, email_html, html_to_text, label,
                       render_body, render_cover, render_subject, template_problem)

ASSETS = Path(__file__).resolve().parent / "assets"
JOB_FIELDS = ["company", "role", "contact_name", "email", "template", "cover", "resume",
              "source", "job_url", "location", "notes", "status", "sent_at"]
CONFIG_FIELDS = ["sender_name", "reply_to", "phone", "linkedin", "github", "portfolio"]
SESSION_TTL = 12 * 3600
MAX_RESUME_BYTES = 700 * 1024          # Upstash caps a request at about 1 MB, base64 included
GENERAL_TEMPLATE = "general.json"
DEFAULT_TEMPLATE = "application.json"


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


def _seed_docs(folder):
    out = {}
    for path in sorted((ASSETS / folder).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out[path.name] = data
    return out


# --------------------------------------------------------------------------- #
# the queue
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
    return [{k: str(r.get(k) or "") for k in JOB_FIELDS} for r in rows]


def put_jobs(rows):
    clean = []
    for row in rows:
        item = {k: str(row.get(k) or "").strip() for k in JOB_FIELDS}
        if any(item.values()):
            clean.append(item)
    kv_set_json("jobs", clean)
    return len(clean)


# --------------------------------------------------------------------------- #
# emails, cover letters, resumes, settings
# --------------------------------------------------------------------------- #

def get_templates():
    docs = kv_get_json("templates")
    if docs is None:
        docs = _seed_docs("templates")
        kv_set_json("templates", docs)
    return docs


def get_covers():
    docs = kv_get_json("covers")
    if docs is None:
        docs = _seed_docs("covers")
        kv_set_json("covers", docs)
    return docs


def get_resumes():
    """id -> {name, filename}. An older single-resume deployment is carried over as 'main'."""
    docs = kv_get_json("resumes")
    if docs is None:
        docs = {}
        old_name = kv("GET", "resume_name")
        old_file = kv("GET", "resume_b64") if old_name else None
        if old_file:
            docs["main.json"] = {"name": "Main resume", "filename": old_name}
            kv("SET", "resume_file:main.json", old_file)
        kv_set_json("resumes", docs)
    return docs


def get_config():
    return kv_get_json("config") or {}


def save_config(cfg):
    kv_set_json("config", cfg)


def doc_id(name, taken, fallback):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or fallback
    slug, n = base, 2
    while slug + ".json" in taken:
        slug, n = "%s-%d" % (base, n), n + 1
    return slug + ".json"


def template_view(tid, data):
    return {"id": tid, "name": str(data.get("name") or tid), "subject": str(data.get("subject") or ""),
            "greeting": str(data.get("greeting") or ""), "html": str(data.get("html") or ""),
            "no_company": bool(data.get("no_company"))}


def list_templates(docs):
    out = [template_view(i, d) for i, d in docs.items()]
    out = [{k: t[k] for k in ("id", "name", "subject", "no_company")} for t in out]
    return sorted(out, key=lambda t: t["name"].lower())


def list_covers(docs):
    out = [{"id": i, "name": str(d.get("name") or i),
            "words": len(html_to_text(str(d.get("html") or "")).split())} for i, d in docs.items()]
    return sorted(out, key=lambda c: c["name"].lower())


def list_resumes(docs):
    out = [{"id": rid, "name": str(d.get("name") or rid), "path": "",
            "filename": str(d.get("filename") or "resume.pdf"), "ok": True} for rid, d in docs.items()]
    return sorted(out, key=lambda r: r["name"].lower())


def _default_of(cfg, key, fallback, docs, no_company):
    for name in (cfg.get(key), fallback):
        if name in docs and bool(docs[name].get("no_company")) == no_company:
            return name
    names = sorted(i for i, d in docs.items() if bool(d.get("no_company")) == no_company)
    return names[0] if names else ""


def active_template(cfg, tpls):
    return _default_of(cfg, "active_template", DEFAULT_TEMPLATE, tpls, False)


def active_general(cfg, tpls):
    return _default_of(cfg, "active_general_template", GENERAL_TEMPLATE, tpls, True)


def active_cover(cfg, covers):
    name = cfg.get("active_cover") or ""
    return name if name in covers else ""


def active_resume(cfg, resumes):
    name = cfg.get("active_resume") or ""
    if name in resumes:
        return name
    names = sorted(resumes)
    return names[0] if names else ""


def row_picks(row, cfg, tpls, covers, resumes):
    """The email, cover letter ("" for none) and resume this row goes out with."""
    if (row.get("company") or "").strip():
        default = active_template(cfg, tpls) or active_general(cfg, tpls)
    else:
        default = active_general(cfg, tpls) or active_template(cfg, tpls)
    tpl = row.get("template") or default or DEFAULT_TEMPLATE
    cover = row.get("cover") or active_cover(cfg, covers)
    cover = "" if cover.lower() == "none" else cover
    resume = row.get("resume") or active_resume(cfg, resumes)
    return tpl, cover, resume


def pick_problem(row, cfg, tpls, covers, resumes):
    tpl, cover, resume = row_picks(row, cfg, tpls, covers, resumes)
    if tpl not in tpls:
        return "email missing"
    if cover and cover not in covers:
        return "cover letter missing"
    if not resume or resume not in resumes:
        return "no resume"
    return None


def resume_bytes(rid):
    encoded = kv("GET", "resume_file:" + rid)
    if not encoded:
        raise Abort("The resume file is missing — upload it again on the Resume tab.")
    return base64.b64decode(encoded)


# --------------------------------------------------------------------------- #
# the sent log
# --------------------------------------------------------------------------- #

def get_log():
    return kv_get_json("sent_log", []) or []


def append_log(entry):
    entries = get_log()
    entries.append(entry)
    kv_set_json("sent_log", entries[-1000:])


def sent_pairs(log):
    seen = set()
    for entry in log:
        if entry.get("result") == "sent":
            seen.add((str(entry.get("email", "")).lower().strip(),
                      str(entry.get("company", "")).lower().strip()))
    return seen


# --------------------------------------------------------------------------- #
# time — the sender's clock, not the server's
# --------------------------------------------------------------------------- #

def local_now(tz_offset_minutes=0):
    try:
        offset = int(tz_offset_minutes)
    except (TypeError, ValueError):
        offset = 0
    offset = max(-840, min(840, offset))
    return datetime.utcnow() + timedelta(minutes=offset)


# --------------------------------------------------------------------------- #
# building a message
# --------------------------------------------------------------------------- #

def build_message(row, cfg, tpl, resume, cover=None, to_addr=None, now=None):
    """tpl and cover are stored documents; resume is (bytes, attachment filename)."""
    body = render_body(tpl, row, cfg, now)
    text = html_to_text(body)
    subject = render_subject(tpl, row, cfg, now)
    problem = template_problem(subject, text)
    if problem:
        raise Abort(problem)
    msg = EmailMessage()
    msg["From"] = "%s <%s>" % (cfg.get("sender_name") or cfg["sender_email"], cfg["sender_email"])
    msg["To"] = to_addr or (row.get("email") or "").strip()
    msg["Subject"] = subject
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    msg.set_content(text)
    msg.add_alternative(email_html(body), subtype="html")
    data, filename = resume
    msg.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    if cover:
        cover_body = render_cover(cover, row, cfg, now)
        problem = template_problem(html_to_text(cover_body))
        if problem:
            raise Abort('Cover letter "%s": %s' % (cover.get("name") or "", problem))
        pdf = cover_pdf.render(cover_body, title="Cover letter — " + (row.get("company") or "").strip())
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=cover_filename(cfg))
    return msg


# --------------------------------------------------------------------------- #
# sending
# --------------------------------------------------------------------------- #

def smtp_send(msg, cfg):
    password = "".join(os.environ.get("GMAIL_APP_PASSWORD", "").split())
    if not password:
        raise Abort("GMAIL_APP_PASSWORD is not set in your Vercel environment variables.")
    if not password.isascii():
        raise Abort("GMAIL_APP_PASSWORD has characters in it that are not plain letters. "
                    "Paste the 16 letters again in Vercel.")
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=25) as server:
            server.login(cfg["sender_email"], password)
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
    payload = {
        "from": os.environ.get("RESEND_FROM", ""),
        "to": [msg["To"]],
        "subject": msg["Subject"],
        "text": msg.get_body(preferencelist=("plain",)).get_content(),
        "html": msg.get_body(preferencelist=("html",)).get_content(),
        "reply_to": cfg.get("reply_to") or cfg["sender_email"],
        "attachments": [{"filename": part.get_filename() or "attachment.pdf",
                         "content": base64.b64encode(part.get_payload(decode=True)).decode("ascii")}
                        for part in msg.iter_attachments()],
    }
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


def sender_config(stored):
    cfg = {k: str(stored.get(k) or "") for k in CONFIG_FIELDS}
    cfg["sender_email"] = os.environ.get("GMAIL_ADDRESS", "").strip()
    return cfg


def send_one(payload):
    """Sends the one row whose email and company match, and records it."""
    email = str(payload.get("email") or "").strip().lower()
    company = str(payload.get("company") or "").strip().lower()
    if not email:
        raise Abort("No row given to send.")
    stored = get_config()
    cfg = sender_config(stored)
    if not cfg["sender_email"]:
        raise Abort("GMAIL_ADDRESS is not set in your Vercel environment variables.")
    if not cfg["sender_name"]:
        raise Abort("Fill in your name on the Setup tab first.")
    rows, tpls, covers, resumes = get_jobs(), get_templates(), get_covers(), get_resumes()
    seen = sent_pairs(get_log())
    row = next((r for r in rows if r["email"].strip().lower() == email
                and r["company"].strip().lower() == company
                and (r.get("status") or "").strip().lower() != "sent"), None)
    if row is None:
        raise Abort("That row is not in the queue, or it was already sent.")
    reason = check(row, seen) or pick_problem(row, stored, tpls, covers, resumes)
    if reason:
        raise Abort("%s was not sent: %s." % (label(row), reason))
    tpl_id, cover_id, resume_id = row_picks(row, stored, tpls, covers, resumes)
    now = local_now(payload.get("tz"))
    resume = (resume_bytes(resume_id), resumes[resume_id].get("filename") or "resume.pdf")
    msg = build_message(row, cfg, tpls[tpl_id], resume, covers[cover_id] if cover_id else None, now=now)
    entry = {"sent_at": now.strftime("%Y-%m-%d %H:%M:%S"), "company": row["company"],
             "role": row["role"], "email": msg["To"], "subject": msg["Subject"],
             "source": row["source"], "job_url": row["job_url"]}
    try:
        deliver(msg, cfg)
    except Abort as exc:
        append_log({**entry, "result": "failed: %s" % exc})
        raise
    append_log({**entry, "result": "sent"})
    row["status"], row["sent_at"] = "sent", now.strftime("%Y-%m-%d %H:%M")
    kv_set_json("jobs", rows)
    return {"ok": True, "sent_at": row["sent_at"],
            "attachments": [p.get_filename() for p in msg.iter_attachments()]}


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
    if not hmac.compare_digest(stored.encode("utf-8"), str(password or "").encode("utf-8")):
        return None
    return make_token()


def login(payload):
    token = check_login(payload.get("password"))
    if not token:
        raise Abort("Wrong password")
    return {"token": token}


# --------------------------------------------------------------------------- #
# what the page loads on every refresh
# --------------------------------------------------------------------------- #

def config_state(stored, resumes):
    cfg = sender_config(stored)
    out = dict(cfg)
    out["has_password"] = bool(os.environ.get("GMAIL_APP_PASSWORD")) or bool(
        os.environ.get("RESEND_API_KEY") and os.environ.get("RESEND_FROM"))
    missing = []
    if not cfg["sender_email"]:
        missing.append("GMAIL_ADDRESS (in Vercel)")
    if not cfg["sender_name"]:
        missing.append("your name (Setup tab)")
    if not out["has_password"]:
        missing.append("GMAIL_APP_PASSWORD (in Vercel)")
    if not resumes:
        missing.append("a resume (Resume tab)")
    out["missing"] = missing
    out["ready"] = not missing
    return out


def state(_payload=None):
    if not storage_ready():
        raise Abort("No storage connected. In the Vercel dashboard open Storage, create an Upstash "
                    "Redis database and connect it to this project, then redeploy.")
    stored = get_config()
    rows, tpls, covers, resumes, log = get_jobs(), get_templates(), get_covers(), get_resumes(), get_log()
    seen = sent_pairs(log)
    jobs = []
    for row in rows:
        item = dict(row)
        item["_skip"] = check(row, seen) or pick_problem(row, stored, tpls, covers, resumes) or ""
        jobs.append(item)
    ready = sum(1 for j in jobs if not j["_skip"])
    sent = sum(1 for j in jobs if (j.get("status") or "").lower() == "sent")
    return {
        "mode": "online",
        "config": config_state(stored, resumes),
        "jobs": jobs,
        "header": JOB_FIELDS,
        "counts": {"total": len(jobs), "ready": ready, "blocked": len(jobs) - ready, "sent": sent},
        "templates": list_templates(tpls),
        "active_template": active_template(stored, tpls),
        "active_general_template": active_general(stored, tpls),
        "covers": list_covers(covers),
        "active_cover": active_cover(stored, covers),
        "resumes": list_resumes(resumes),
        "active_resume": active_resume(stored, resumes),
        "placeholders": PLACEHOLDERS,
        "sent_log": log[-500:],
        "defaults": {"limit": 25, "delay": 45},
    }


# --------------------------------------------------------------------------- #
# editing — queue, settings, emails, covers, resumes
# --------------------------------------------------------------------------- #

def save_settings(patch):
    stored = get_config()
    for key in CONFIG_FIELDS:
        if key in patch:
            stored[key] = str(patch[key]).strip()
    save_config(stored)
    return {"ok": True}


def save_jobs(payload):
    return {"ok": True, "saved": put_jobs(payload.get("rows") or [])}


def get_template(payload):
    tid = str(payload.get("name") or "")
    docs = get_templates()
    if tid not in docs:
        raise Abort("no such template")
    return template_view(tid, docs[tid])


def save_template(payload):
    name = str(payload.get("name") or "").strip()
    subject = str(payload.get("subject") or "").strip()
    if not name:
        raise Abort("Give the email a name.")
    if not subject:
        raise Abort("The subject line cannot be empty.")
    docs = get_templates()
    tid = str(payload.get("id") or "")
    if tid and tid not in docs:
        raise Abort("That email no longer exists — reload the page.")
    tid = tid or doc_id(name, docs, "email")
    data = {"name": name, "subject": subject, "greeting": str(payload.get("greeting") or "").strip(),
            "html": str(payload.get("html") or "")}
    if payload.get("no_company"):
        data["no_company"] = True
    docs[tid] = data
    kv_set_json("templates", docs)
    return {"ok": True, "id": tid}


def default_key(doc):
    return "active_general_template" if doc.get("no_company") else "active_template"


def delete_template(payload):
    tid = str(payload.get("id") or "")
    docs = get_templates()
    if tid not in docs:
        raise Abort("That email no longer exists — reload the page.")
    if len(docs) <= 1:
        raise Abort("This is your only email. Make another one before deleting it.")
    doc = docs.pop(tid)
    kv_set_json("templates", docs)
    stored = get_config()
    if stored.get(default_key(doc)) == tid:
        stored.pop(default_key(doc))
        save_config(stored)
    return {"ok": True}


def use_template(payload):
    tid = str(payload.get("id") or "")
    docs = get_templates()
    if tid not in docs:
        raise Abort("That email no longer exists — reload the page.")
    stored = get_config()
    stored[default_key(docs[tid])] = tid
    save_config(stored)
    return {"ok": True}


def preview_context(no_company=False):
    """Previews are general: your own details filled in, the company's shown as [Company]."""
    cfg = sender_config(get_config())
    cfg["sender_name"] = cfg["sender_name"] or "Your Name"
    cfg["sender_email"] = cfg["sender_email"] or "you@gmail.com"
    return cfg, {"company": "" if no_company else "[Company]", "role": "[Role]", "email": ""}


def preview_template(payload):
    cfg, row = preview_context(bool(payload.get("no_company")))
    stored, covers, resumes = get_config(), get_covers(), get_resumes()
    now = local_now(payload.get("tz"))
    tpl = {k: str(payload.get(k) or "") for k in ("subject", "greeting", "html")}
    body = render_body(tpl, row, cfg, now)
    subject = render_subject(tpl, row, cfg, now)
    text = html_to_text(body)
    rid = active_resume(stored, resumes)
    attachments = [str(resumes[rid].get("filename") or "resume.pdf")] if rid else []
    if active_cover(stored, covers):
        attachments.append(cover_filename(cfg))
    return {"from": "%s <%s>" % (cfg["sender_name"], cfg["sender_email"]),
            "to": "[the company's email address]", "subject": subject, "html": email_html(body),
            "text": text, "attachments": attachments,
            "problem": template_problem(subject, text) or ""}


def get_cover(payload):
    cid = str(payload.get("name") or "")
    docs = get_covers()
    if cid not in docs:
        raise Abort("no such cover letter")
    return {"id": cid, "name": str(docs[cid].get("name") or cid), "html": str(docs[cid].get("html") or "")}


def save_cover(payload):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise Abort("Give the cover letter a name.")
    docs = get_covers()
    cid = str(payload.get("id") or "")
    if cid and cid not in docs:
        raise Abort("That cover letter no longer exists — reload the page.")
    cid = cid or doc_id(name, docs, "cover")
    docs[cid] = {"name": name, "html": str(payload.get("html") or "")}
    kv_set_json("covers", docs)
    return {"ok": True, "id": cid}


def delete_cover(payload):
    cid = str(payload.get("id") or "")
    docs = get_covers()
    if cid not in docs:
        raise Abort("That cover letter no longer exists — reload the page.")
    docs.pop(cid)
    kv_set_json("covers", docs)
    stored = get_config()
    if stored.get("active_cover") == cid:
        stored.pop("active_cover")
        save_config(stored)
    return {"ok": True}


def use_cover(payload):
    """An empty id means send without a cover letter."""
    cid = str(payload.get("id") or "")
    stored = get_config()
    if cid:
        if cid not in get_covers():
            raise Abort("That cover letter no longer exists — reload the page.")
        stored["active_cover"] = cid
    else:
        stored.pop("active_cover", None)
    save_config(stored)
    return {"ok": True}


def preview_cover(payload):
    cfg, row = preview_context()
    body = render_cover({"html": str(payload.get("html") or "")}, row, cfg, local_now(payload.get("tz")))
    return {"html": body, "filename": cover_filename(cfg),
            "problem": template_problem(html_to_text(body)) or ""}


def preview_cover_pdf(payload):
    cfg, row = preview_context()
    body = render_cover({"html": str(payload.get("html") or "")}, row, cfg, local_now(payload.get("tz")))
    return cover_pdf.render(body, title="Cover letter"), "application/pdf"


def save_resume(payload):
    """A new resume needs an uploaded PDF ("data", base64). Renaming one needs no upload."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise Abort("Give the resume a name.")
    docs = get_resumes()
    rid = str(payload.get("id") or "")
    if rid and rid not in docs:
        raise Abort("That resume no longer exists — reload the page.")
    data = str(payload.get("data") or "")
    if not rid and not data:
        raise Abort("Upload a PDF for this resume.")
    rid = rid or doc_id(name, docs, "resume")
    if data:
        if data.lstrip().startswith("data:"):
            data = data.split(",", 1)[1]
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError:
            raise Abort("That upload could not be read — try again.")
        if not raw.startswith(b"%PDF"):
            raise Abort("That file is not a PDF.")
        if len(raw) > MAX_RESUME_BYTES:
            raise Abort("That PDF is %d KB; the online version takes up to %d KB. Export a lighter "
                        "copy and upload that." % (len(raw) // 1024, MAX_RESUME_BYTES // 1024))
        kv("SET", "resume_file:" + rid, base64.b64encode(raw).decode("ascii"))
    filename = str(payload.get("filename") or "").strip() or docs.get(rid, {}).get("filename") or "resume.pdf"
    docs[rid] = {"name": name, "filename": filename}
    kv_set_json("resumes", docs)
    return {"ok": True, "id": rid}


def delete_resume(payload):
    rid = str(payload.get("id") or "")
    docs = get_resumes()
    if rid not in docs:
        raise Abort("That resume no longer exists — reload the page.")
    if len(docs) <= 1:
        raise Abort("This is your only resume. Add another one before deleting it.")
    docs.pop(rid)
    kv_set_json("resumes", docs)
    kv("DEL", "resume_file:" + rid)
    stored = get_config()
    if stored.get("active_resume") == rid:
        stored.pop("active_resume")
        save_config(stored)
    return {"ok": True}


def use_resume(payload):
    rid = str(payload.get("id") or "")
    if rid not in get_resumes():
        raise Abort("That resume no longer exists — reload the page.")
    stored = get_config()
    stored["active_resume"] = rid
    save_config(stored)
    return {"ok": True}


def resume_file(payload):
    rid = str(payload.get("name") or "")
    if rid not in get_resumes():
        raise Abort("no such resume file")
    return resume_bytes(rid), "application/pdf"
