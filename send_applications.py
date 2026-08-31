#!/usr/bin/env python3
"""
Job application mailer.

Reads jobs.csv, renders a personalised email per row from a template,
attaches your resume, and sends it through Gmail SMTP.

Safe by default: it does NOTHING unless you pass --send.

    python3 send_applications.py                 # dry run, writes previews/
    python3 send_applications.py --test          # send one email to yourself
    python3 send_applications.py --send          # send for real
    python3 send_applications.py --send --limit 10 --delay 60
"""

import argparse
import csv
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
JOBS_PATH = ROOT / "jobs.csv"
LOG_PATH = ROOT / "sent_log.csv"
PREVIEW_DIR = ROOT / "previews"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
LOG_FIELDS = ["sent_at", "company", "role", "email", "subject", "source", "job_url", "result"]


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def load_config():
    if not CONFIG_PATH.exists():
        die(f"Missing {CONFIG_PATH.name}. Copy config.example.json to config.json and fill it in.")
    cfg = json.loads(CONFIG_PATH.read_text())
    required = ["sender_name", "sender_email", "app_password", "resume_path"]
    missing = [k for k in required if not cfg.get(k) or str(cfg[k]).startswith("PUT_")]
    if missing:
        die("config.json is not filled in yet: " + ", ".join(missing))
    resume = Path(os.path.expanduser(cfg["resume_path"]))
    if not resume.exists():
        die(f"Resume not found at {resume}")
    cfg["resume_path"] = resume
    return cfg


def load_jobs():
    if not JOBS_PATH.exists():
        die("jobs.csv not found.")
    with JOBS_PATH.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return rows


def load_template(name):
    path = ROOT / "templates" / name
    if not path.exists():
        die(f"Template not found: {path}")
    raw = path.read_text(encoding="utf-8")
    if not raw.lower().startswith("subject:"):
        die(f"{name} must start with a 'Subject: ...' line.")
    subject_line, _, body = raw.partition("\n")
    return subject_line.split(":", 1)[1].strip(), body.lstrip("\n")


def already_sent_addresses():
    """Every address we have successfully mailed before — belt and braces
    against sending the same person two applications."""
    seen = set()
    if LOG_PATH.exists():
        with LOG_PATH.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("result") == "sent":
                    seen.add((row.get("email", "").lower().strip(),
                              row.get("company", "").lower().strip()))
    return seen


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def render(text, row, cfg):
    """Replace {{field}} placeholders. Falls back sensibly on blanks."""
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
        "sender_name": cfg["sender_name"],
        "sender_email": cfg["sender_email"],
        "phone": cfg.get("phone", ""),
        "linkedin": cfg.get("linkedin", ""),
        "github": cfg.get("github", ""),
        "portfolio": cfg.get("portfolio", ""),
    }
    out = text
    for key, val in values.items():
        out = out.replace("{{" + key + "}}", val)

    # A line whose only content was an empty placeholder (e.g. no job_url)
    # is dropped rather than left dangling.
    kept = [ln for ln in out.split("\n") if not re.match(r"^\s*(Role link:|Source:)\s*$", ln)]
    leftover = re.findall(r"\{\{(\w+)\}\}", "\n".join(kept))
    if leftover:
        die(f"Template has unknown placeholders: {', '.join(sorted(set(leftover)))}")
    return "\n".join(kept)


def build_message(row, cfg, subject_tpl, body_tpl, to_addr=None):
    msg = EmailMessage()
    msg["From"] = f'{cfg["sender_name"]} <{cfg["sender_email"]}>'
    msg["To"] = to_addr or row["email"].strip()
    msg["Subject"] = render(subject_tpl, row, cfg)
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    msg.set_content(render(body_tpl, row, cfg))

    resume = cfg["resume_path"]
    msg.add_attachment(
        resume.read_bytes(),
        maintype="application",
        subtype="pdf" if resume.suffix.lower() == ".pdf" else "octet-stream",
        filename=cfg.get("resume_filename") or resume.name,
    )
    return msg


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def check(row, seen):
    """Return a reason string if this row must be skipped, else None."""
    status = (row.get("status") or "").strip().lower()
    if status == "sent":
        return "already marked sent"
    if status in {"skip", "hold", "no"}:
        return f"status={status}"
    if not (row.get("company") or "").strip():
        return "no company"
    email = (row.get("email") or "").strip()
    if not email:
        return "no email address"
    if not EMAIL_RE.match(email):
        return f"malformed email ({email})"
    if (email.lower(), (row.get("company") or "").lower().strip()) in seen:
        return "already in sent_log.csv"
    return None


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def write_preview(idx, row, msg):
    PREVIEW_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", (row.get("company") or "unknown"))[:40]
    path = PREVIEW_DIR / f"{idx:03d}_{safe}.txt"
    path.write_text(
        f"To:      {msg['To']}\n"
        f"From:    {msg['From']}\n"
        f"Subject: {msg['Subject']}\n"
        f"Attach:  {row['_attachment']}\n"
        + "-" * 70 + "\n"
        + msg.get_body(preferencelist=("plain",)).get_content(),
        encoding="utf-8",
    )
    return path


def log_send(row, msg, result):
    new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({
            "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "company": row.get("company", ""),
            "role": row.get("role", ""),
            "email": msg["To"],
            "subject": msg["Subject"],
            "source": row.get("source", ""),
            "job_url": row.get("job_url", ""),
            "result": result,
        })


def save_jobs(rows, fieldnames):
    tmp = JOBS_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    tmp.replace(JOBS_PATH)


def die(msg):
    print(f"\n  ERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(description="Send job application emails with your resume attached.")
    p.add_argument("--send", action="store_true", help="actually send (default is a dry run)")
    p.add_argument("--test", action="store_true", help="send ONE email to yourself and stop")
    p.add_argument("--limit", type=int, default=25, help="max emails this run (default 25)")
    p.add_argument("--delay", type=int, default=45, help="seconds between sends (default 45)")
    p.add_argument("--template", default="application.txt", help="template file in templates/")
    p.add_argument("--only", help="only rows whose company contains this text")
    args = p.parse_args()

    cfg = load_config()
    subject_tpl, body_tpl = load_template(args.template)
    rows = load_jobs()
    if not rows:
        die("jobs.csv has no rows yet. Add some listings first.")
    fieldnames = list(rows[0].keys())
    for extra in ("status", "sent_at"):
        if extra not in fieldnames:
            fieldnames.append(extra)

    seen = already_sent_addresses()

    # --- test mode: one email to yourself ---------------------------------- #
    if args.test:
        sample = dict(rows[0])
        sample.setdefault("company", "Test Company")
        sample["_attachment"] = cfg["resume_path"].name
        msg = build_message(sample, cfg, subject_tpl, body_tpl, to_addr=cfg["sender_email"])
        deliver([(sample, msg)], cfg, delay=0)
        print(f"\n  Test email sent to {cfg['sender_email']} — check the attachment opens.\n")
        return

    # --- select ------------------------------------------------------------ #
    queue, skipped = [], []
    for i, row in enumerate(rows, start=1):
        if args.only and args.only.lower() not in (row.get("company") or "").lower():
            continue
        reason = check(row, seen)
        if reason:
            skipped.append((row.get("company", "?"), reason))
            continue
        if len(queue) >= args.limit:
            skipped.append((row.get("company", "?"), f"over --limit {args.limit}"))
            continue
        row["_attachment"] = cfg["resume_path"].name
        queue.append((row, build_message(row, cfg, subject_tpl, body_tpl)))
        seen.add((row["email"].lower().strip(), (row.get("company") or "").lower().strip()))

    print(f"\n  Template : {args.template}")
    print(f"  Resume   : {cfg['resume_path']}")
    print(f"  From     : {cfg['sender_name']} <{cfg['sender_email']}>")
    print(f"  Ready    : {len(queue)}    Skipped: {len(skipped)}\n")
    for company, reason in skipped[:15]:
        print(f"    skip  {company:<32} {reason}")
    if len(skipped) > 15:
        print(f"    ... and {len(skipped) - 15} more")

    if not queue:
        print("\n  Nothing to send.\n")
        return

    # --- dry run ----------------------------------------------------------- #
    if not args.send:
        for i, (row, msg) in enumerate(queue, start=1):
            write_preview(i, row, msg)
        print(f"\n  DRY RUN — nothing was sent.")
        print(f"  {len(queue)} previews written to previews/")
        print(f"  Read a couple, then run again with --send\n")
        return

    # --- real send --------------------------------------------------------- #
    results = deliver(queue, cfg, delay=args.delay)
    for row, ok in results:
        if ok:
            row["status"] = "sent"
            row["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_jobs(rows, fieldnames)
    good = sum(1 for _, ok in results if ok)
    print(f"\n  Done. {good}/{len(results)} sent. jobs.csv and sent_log.csv updated.\n")


def deliver(queue, cfg, delay):
    """Open one SMTP connection and send everything through it."""
    results = []
    ctx = ssl.create_default_context()
    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=60)
    except Exception as exc:
        die(f"Could not reach Gmail SMTP: {exc}")
    try:
        try:
            server.login(cfg["sender_email"], cfg["app_password"].replace(" ", ""))
        except smtplib.SMTPAuthenticationError:
            die("Gmail rejected the login. The app_password must be a 16-character "
                "Google App Password (not your normal password), and 2-Step "
                "Verification must be on for that account.")
        for i, (row, msg) in enumerate(queue, start=1):
            label = f'{row.get("company", "?")} <{msg["To"]}>'
            try:
                server.send_message(msg)
                print(f"  [{i}/{len(queue)}] sent    {label}")
                log_send(row, msg, "sent")
                results.append((row, True))
            except Exception as exc:
                print(f"  [{i}/{len(queue)}] FAILED  {label} — {exc}")
                log_send(row, msg, f"failed: {exc}")
                results.append((row, False))
            if delay and i < len(queue):
                time.sleep(delay)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return results


if __name__ == "__main__":
    main()
