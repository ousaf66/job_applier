#!/usr/bin/env python3
"""
Job application mailer.

Reads jobs.csv, renders a personalised email per row from a template,
attaches your resume, and sends it through Gmail SMTP.

Templates live in templates/*.json — a name, a subject line, a greeting and an
HTML body. Each email goes out as HTML with a plain-text copy for clients that
do not show HTML. Templates with "no_company": true are for rows that have no
company name; the rest are for rows that do.

Cover letters live in covers/*.json — a name and an HTML body, filled in per company
like the emails and attached as a PDF next to your resume.

Resumes live in resumes/*.json — a name, the path to the PDF, and the filename the
company sees.

Each row in jobs.csv can pick its own email, cover letter ("none" for no cover) and
resume in the template / cover / resume columns. A blank column means the default
marked in config.json: active_template for rows with a company, active_general_template
for rows without one, then active_cover and active_resume. --template, --cover and
--resume override every row for one run.

Safe by default: it does NOTHING unless you pass --send.

    python3 send_applications.py                 # dry run, writes previews/
    python3 send_applications.py --test          # send one email to yourself
    python3 send_applications.py --send          # send for real
    python3 send_applications.py --send --limit 10 --delay 60
    python3 send_applications.py --template short.json
    python3 send_applications.py --cover none     # no cover letter this run
    python3 send_applications.py --resume main    # this resume for every row
"""

import argparse
import csv
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

import cover_pdf

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
JOBS_PATH = ROOT / "jobs.csv"
LOG_PATH = ROOT / "sent_log.csv"
PREVIEW_DIR = ROOT / "previews"
TEMPLATE_DIR = ROOT / "templates"
COVER_DIR = ROOT / "covers"
RESUME_DIR = ROOT / "resumes"
DEFAULT_TEMPLATE = "application.json"
GENERAL_TEMPLATE = "general.json"           # first choice for rows with no company

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
LOG_FIELDS = ["sent_at", "company", "role", "email", "subject", "source", "job_url", "result"]


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def load_config():
    if not CONFIG_PATH.exists():
        die(f"Missing {CONFIG_PATH.name}. Copy config.example.json to config.json and fill it in.")
    cfg = json.loads(CONFIG_PATH.read_text())
    required = ["sender_name", "sender_email", "app_password"]
    missing = [k for k in required if not cfg.get(k) or str(cfg[k]).startswith("PUT_")]
    if missing:
        die("config.json is not filled in yet: " + ", ".join(missing))
    adopt_config_resume(cfg)
    return cfg


def load_jobs():
    if not JOBS_PATH.exists():
        die("jobs.csv not found.")
    with JOBS_PATH.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return rows


def doc_path(folder, name):
    name = os.path.basename(str(name or ""))
    if name and not name.endswith(".json"):
        name += ".json"
    return folder / name


def template_path(name):
    return doc_path(TEMPLATE_DIR, name)


def cover_path(name):
    return doc_path(COVER_DIR, name)


def read_doc(path):
    """A template or cover file as a dict, or None if missing or not valid JSON."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def read_template(name):
    """The raw template dict, or None if it does not exist or is not valid JSON."""
    path = template_path(name)
    data = read_doc(path)
    if data is None:
        return None
    return {
        "id": path.name,
        "name": str(data.get("name") or path.stem),
        "subject": str(data.get("subject") or ""),
        "greeting": str(data.get("greeting") or ""),
        "html": str(data.get("html") or ""),
        "no_company": bool(data.get("no_company")),
    }


def load_template(name):
    tpl = read_template(name)
    if tpl is None:
        die(f"Template not found or not valid JSON: {template_path(name)}")
    if not tpl["subject"].strip():
        die(f"{tpl['id']} has no subject line.")
    return tpl


def list_templates():
    out = []
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        tpl = read_template(path.name)
        if tpl:
            out.append({"id": tpl["id"], "name": tpl["name"], "subject": tpl["subject"],
                        "no_company": tpl["no_company"]})
    return sorted(out, key=lambda t: t["name"].lower())


def _default_of(cfg, key, fallback, no_company):
    """The default email of one kind, falling back to any email of that kind ("" if none)."""
    for name in (cfg.get(key), fallback):
        tpl = read_template(name) if name else None
        if tpl and tpl["no_company"] == no_company:
            return tpl["id"]
    names = [t["id"] for t in list_templates() if t["no_company"] == no_company]
    return names[0] if names else ""


def active_template_name(cfg):
    """The default email for rows that have a company name."""
    return _default_of(cfg, "active_template", DEFAULT_TEMPLATE, False)


def active_general_name(cfg):
    """The default email for rows with no company name."""
    return _default_of(cfg, "active_general_template", GENERAL_TEMPLATE, True)


def read_cover(name):
    path = cover_path(name)
    data = read_doc(path)
    if data is None:
        return None
    return {"id": path.name, "name": str(data.get("name") or path.stem),
            "html": str(data.get("html") or "")}


def load_cover(name):
    cover = read_cover(name)
    if cover is None:
        die(f"Cover letter not found or not valid JSON: {cover_path(name)}")
    return cover


def list_covers():
    out = []
    for path in sorted(COVER_DIR.glob("*.json")):
        cover = read_cover(path.name)
        if cover:
            words = len(html_to_text(cover["html"]).split())
            out.append({"id": cover["id"], "name": cover["name"], "words": words})
    return sorted(out, key=lambda c: c["name"].lower())


def active_cover_name(cfg):
    """The cover letter marked in use, or "" to send without one."""
    name = cfg.get("active_cover") or ""
    return cover_path(name).name if name and read_cover(name) else ""


def resume_doc(name):
    return doc_path(RESUME_DIR, name)


def read_resume(name):
    path = resume_doc(name)
    data = read_doc(path)
    if data is None:
        return None
    raw = str(data.get("path") or "").strip()
    file = Path(os.path.expanduser(raw))
    return {"id": path.name, "name": str(data.get("name") or path.stem), "path": raw,
            "file": file, "filename": str(data.get("filename") or "").strip() or file.name,
            "ok": bool(raw) and file.is_file()}


def load_resume(name):
    res = read_resume(name)
    if res is None:
        die(f"Resume not found or not valid JSON: {resume_doc(name)}")
    if not res["ok"]:
        die(f'Resume "{res["name"]}": no file at {res["file"]}')
    return res


def list_resumes():
    out = []
    for path in sorted(RESUME_DIR.glob("*.json")):
        res = read_resume(path.name)
        if res:
            out.append({k: res[k] for k in ("id", "name", "path", "filename", "ok")})
    return sorted(out, key=lambda r: r["name"].lower())


def active_resume_name(cfg):
    """The default resume, falling back to any resume that exists."""
    name = cfg.get("active_resume") or ""
    if name and read_resume(name):
        return resume_doc(name).name
    names = [r["id"] for r in list_resumes()]
    return names[0] if names else ""


def adopt_config_resume(cfg):
    """Older setups kept one resume in config.json. Turn it into resumes/main.json."""
    path = str(cfg.get("resume_path") or "").strip()
    if not path or any(RESUME_DIR.glob("*.json")):
        return
    RESUME_DIR.mkdir(exist_ok=True)
    data = {"name": "Main resume", "path": path,
            "filename": cfg.get("resume_filename") or os.path.basename(path)}
    resume_doc("main").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")


def default_template_name(cfg, row):
    """Rows with a company get that kind's default email, rows without one the other
    kind's; if a kind has no emails at all, the other kind's default stands in."""
    if (row.get("company") or "").strip():
        return active_template_name(cfg) or active_general_name(cfg) or DEFAULT_TEMPLATE
    return active_general_name(cfg) or active_template_name(cfg) or DEFAULT_TEMPLATE


def row_picks(row, cfg, override=None):
    """The email, cover letter ("" for none) and resume this row goes out with.
    The row's own picks beat the defaults; override (the CLI flags) beats both."""
    o = override or {}
    tpl = o.get("template") or row.get("template") or default_template_name(cfg, row)
    cover = o["cover"] if o.get("cover") is not None else (row.get("cover") or active_cover_name(cfg))
    resume = o.get("resume") or row.get("resume") or active_resume_name(cfg)
    return tpl, "" if cover.lower() == "none" else cover, resume


def pick_problem(row, cfg, override=None):
    """Why this row's email, cover or resume cannot be used, else None."""
    tpl, cover, resume = row_picks(row, cfg, override)
    if not read_template(tpl):
        return "email missing"
    if cover and not read_cover(cover):
        return "cover letter missing"
    res = read_resume(resume) if resume else None
    if not res:
        return "no resume"
    if not res["ok"]:
        return "resume file missing"
    return None


def load_picks(row, cfg, override=None):
    tpl, cover, resume = row_picks(row, cfg, override)
    return load_template(tpl), load_cover(cover) if cover else None, load_resume(resume)


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

PLACEHOLDERS = ["company", "role", "contact_name", "greeting_name", "location", "source",
                "job_url", "notes", "sender_name", "sender_email", "phone", "linkedin",
                "github", "portfolio", "date"]


def placeholder_values(row, cfg):
    """What each {{field}} becomes for this row. Falls back sensibly on blanks."""
    contact = (row.get("contact_name") or "").strip()
    today = datetime.now()
    return {
        "company": (row.get("company") or "").strip() or "your company",
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
        "date": f"{today.day} {today:%B %Y}",
    }


def fill(text, values, escape=False):
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", html.escape(val) if escape else val)
    return text


def render_subject(tpl, row, cfg):
    subject = fill(tpl["subject"], placeholder_values(row, cfg))
    return " ".join(subject.split())


def render_body(tpl, row, cfg):
    """The HTML body for this row: the greeting line, then the template body."""
    values = placeholder_values(row, cfg)
    out = ""
    greeting = tpl.get("greeting", "").strip()
    if greeting:
        out += "<p>%s %s,</p>\n" % (html.escape(greeting), html.escape(values["greeting_name"]))
    out += fill(tpl.get("html", ""), values, escape=True)
    return tidy(out)


def tidy(out):
    """A line whose only content was an empty placeholder (no portfolio, say)
    is dropped rather than left as a gap."""
    out = re.sub(r"(?:<br\s*/?>\s*)+(?=</(?:p|div|h\d|li)>)", "", out)
    out = re.sub(r"(?<=<p>)\s*(?:<br\s*/?>\s*)+", "", out)
    out = re.sub(r"(<br\s*/?>\s*){2,}", "<br>", out)
    out = re.sub(r"<p>\s*</p>\s*", "", out)
    return out


def template_problem(*texts):
    """Placeholders that survived rendering — misspelt, or split up by formatting."""
    joined = "\n".join(texts)
    leftover = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", joined))
    unknown = sorted(leftover - set(PLACEHOLDERS))
    if unknown:
        return "Template has unknown placeholders: " + ", ".join(unknown)
    if leftover or "{{" in joined or "}}" in joined:
        return ("Template has a broken placeholder — retype it so the {{ }} and the name "
                "inside have the same formatting.")
    return None


EMAIL_STYLE = "font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;color:#222222"


def email_html(body):
    return ('<!doctype html>\n<html><head><meta charset="utf-8"></head>'
            '<body style="margin:0;padding:0"><div style="%s">\n%s\n</div></body></html>\n'
            % (EMAIL_STYLE, body))


class _TextOnly(HTMLParser):
    """HTML body -> the plain-text copy sent alongside it."""
    BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "table", "tr"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.lists, self.links, self.skip = [], [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.out.append("\n\n----------\n\n")
        elif tag == "li":
            kind = self.lists[-1] if self.lists else ["ul", 0]
            kind[1] += 1
            mark = "%d. " % kind[1] if kind[0] == "ol" else "- "
            self.out.append("\n" + "   " * max(0, len(self.lists) - 1) + mark)
        elif tag == "a":
            self.links.append((dict(attrs).get("href") or "", len(self.out)))
        elif tag in self.BLOCKS:
            self.out.append("\n\n")
        if tag in ("ul", "ol"):
            self.lists.append([tag, 0])

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            text = "".join(self.out[start:])
            bare = re.sub(r"^(https?://|mailto:)", "", href).rstrip("/")
            if href and bare and bare not in text:
                self.out.append(" (%s)" % href)
        elif tag in self.BLOCKS:
            self.out.append("\n\n")
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data.replace("\xa0", " ")))

    def text(self):
        lines = [ln.strip() for ln in "".join(self.out).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def html_to_text(body):
    parser = _TextOnly()
    parser.feed(body)
    parser.close()
    return parser.text()


def render_cover(cover, row, cfg):
    return tidy(fill(cover.get("html", ""), placeholder_values(row, cfg), escape=True))


def cover_filename(cfg):
    name = re.sub(r"[^A-Za-z0-9]+", "_", cfg.get("sender_name") or "").strip("_")
    return (name + "_" if name else "") + "Cover_Letter.pdf"


def build_cover_pdf(cover, row, cfg):
    body = render_cover(cover, row, cfg)
    problem = template_problem(html_to_text(body))
    if problem:
        die(f'Cover letter "{cover["name"]}": {problem}')
    return cover_pdf.render(body, title="Cover letter — " + (row.get("company") or "").strip())


def attachment_names(cfg, cover, resume):
    names = [resume["filename"]]
    if cover:
        names.append(cover_filename(cfg))
    return ", ".join(names)


def build_message(row, cfg, tpl, resume, cover=None, to_addr=None):
    body = render_body(tpl, row, cfg)
    text = html_to_text(body)
    subject = render_subject(tpl, row, cfg)
    problem = template_problem(subject, text)
    if problem:
        die(problem)

    msg = EmailMessage()
    msg["From"] = f'{cfg["sender_name"]} <{cfg["sender_email"]}>'
    msg["To"] = to_addr or row["email"].strip()
    msg["Subject"] = subject
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    msg.set_content(text)
    msg.add_alternative(email_html(body), subtype="html")

    msg.add_attachment(
        resume["file"].read_bytes(),
        maintype="application",
        subtype="pdf" if resume["file"].suffix.lower() == ".pdf" else "octet-stream",
        filename=resume["filename"],
    )
    if cover:
        msg.add_attachment(build_cover_pdf(cover, row, cfg), maintype="application",
                           subtype="pdf", filename=cover_filename(cfg))
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
        return "on hold"
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
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label(row))[:40]
    path = PREVIEW_DIR / f"{idx:03d}_{safe}.html"
    head = "".join(
        '<tr><td style="color:#6b7480;padding:2px 14px 2px 0">%s</td><td>%s</td></tr>'
        % (label, html.escape(str(value)))
        for label, value in (("To", msg["To"]), ("From", msg["From"]),
                             ("Subject", msg["Subject"]), ("Attach", row["_attachment"])))
    body = msg.get_body(preferencelist=("html",)).get_content()
    body = body[body.index("<div"):body.rindex("</div>") + 6]
    path.write_text(
        '<!doctype html>\n<html><head><meta charset="utf-8"><title>%s</title></head>'
        '<body style="margin:0;padding:18px;background:#fff">'
        '<table style="font:13px/1.5 Arial,sans-serif;color:#222;margin-bottom:12px">%s</table>'
        '<hr style="border:0;border-top:1px solid #e3e7ed;margin:0 0 16px">%s</body></html>\n'
        % (html.escape(str(msg["Subject"])), head, body),
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


def label(row):
    """How a row is named in output: its company, or its address when there is none."""
    return (row.get("company") or "").strip() or (row.get("email") or "").strip() or "?"


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
    p.add_argument("--template", help="email in templates/ for every row (default: each row's pick)")
    p.add_argument("--cover", help='cover letter in covers/ for every row, or "none"')
    p.add_argument("--resume", help="resume in resumes/ for every row (default: each row's pick)")
    p.add_argument("--only", help="only rows whose company contains this text")
    args = p.parse_args()

    cfg = load_config()
    override = {"template": args.template, "cover": args.cover, "resume": args.resume}
    rows = load_jobs()
    if not rows:
        die("jobs.csv has no rows yet. Add some listings first.")
    fieldnames = list(rows[0].keys())
    for extra in ("template", "cover", "resume", "status", "sent_at"):
        if extra not in fieldnames:
            fieldnames.append(extra)

    seen = already_sent_addresses()

    # --- test mode: one email to yourself ---------------------------------- #
    if args.test:
        sample = dict(rows[0])
        tpl, cover, resume = load_picks(sample, cfg, override)
        sample["_attachment"] = attachment_names(cfg, cover, resume)
        msg = build_message(sample, cfg, tpl, resume, cover=cover, to_addr=cfg["sender_email"])
        deliver([(sample, msg)], cfg, delay=0)
        print(f"\n  Test email sent to {cfg['sender_email']} — check the attachment opens.\n")
        return

    # --- select ------------------------------------------------------------ #
    queue, skipped = [], []
    for i, row in enumerate(rows, start=1):
        if args.only and args.only.lower() not in (row.get("company") or "").lower():
            continue
        reason = check(row, seen) or pick_problem(row, cfg, override)
        if reason:
            skipped.append((label(row), reason))
            continue
        if len(queue) >= args.limit:
            skipped.append((label(row), f"over --limit {args.limit}"))
            continue
        tpl, cover, resume = load_picks(row, cfg, override)
        row["_attachment"] = attachment_names(cfg, cover, resume)
        row["_picks"] = " · ".join([tpl["name"], cover["name"] if cover else "no cover", resume["name"]])
        queue.append((row, build_message(row, cfg, tpl, resume, cover=cover)))
        seen.add((row["email"].lower().strip(), (row.get("company") or "").lower().strip()))

    print(f"\n  From     : {cfg['sender_name']} <{cfg['sender_email']}>")
    print(f"  Ready    : {len(queue)}    Skipped: {len(skipped)}\n")
    for row, _msg in queue:
        print(f"    send  {label(row):<32} {row['_picks']}")
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


def app_password(raw):
    """Google shows the password as four groups of four, and copying it can bring
    no-break spaces along — drop every kind of space."""
    password = "".join(str(raw or "").split())
    if not password.isascii():
        die("The app password has characters in it that are not plain letters. "
            "Paste it again on the Setup tab.")
    return password


def deliver(queue, cfg, delay):
    """Open one SMTP connection and send everything through it."""
    results = []
    password = app_password(cfg["app_password"])
    ctx = ssl.create_default_context()
    print("  Connecting to Gmail...", flush=True)
    try:
        server = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=60)
    except Exception as exc:
        die(f"Could not reach Gmail SMTP: {exc}")
    try:
        try:
            server.login(cfg["sender_email"], password)
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
