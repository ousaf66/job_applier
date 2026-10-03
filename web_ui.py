#!/usr/bin/env python3
"""
Local web UI for the job application mailer.

    python3 web_ui.py                    # localhost only, link printed below

Edit the queue, design emails and cover letters, and send — with buttons instead
of flags. Dry runs and test sends stay on the CLI. Standard library only.

All the actual work is still done by send_applications.py. This is a front end,
not a second implementation.

SECURITY: this page can send email as you and holds a Gmail app password. It only
binds to 127.0.0.1, and only answers requests carrying the one-time token from the
link it prints at startup.
"""

import argparse
import base64
import contextlib
import csv
import io
import json
import os
import re
import secrets
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cover_pdf
import send_applications as sa

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "web" / "index.html"
URL_FILE = ROOT / ".ui_url"
TOKEN = secrets.token_urlsafe(16)

CONFIG_TEXT_FIELDS = ["sender_name", "sender_email", "reply_to", "phone", "linkedin",
                      "github", "portfolio"]
JOB_FIELDS = ["company", "role", "contact_name", "email", "template", "cover", "resume",
              "source", "job_url", "location", "notes", "status", "sent_at"]
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


class Abort(Exception):
    """send_applications.die() raises this instead of killing the server."""


def _die(msg):
    raise Abort(msg)


sa.die = _die


# --------------------------------------------------------------------------- #
# run state — one job at a time, polled by the page
# --------------------------------------------------------------------------- #

RUN = {"active": False, "mode": "", "lines": [], "error": "", "finished": ""}
LOCK = threading.Lock()


def log(line):
    with LOCK:
        RUN["lines"].append(line)


def run_snapshot():
    with LOCK:
        return {**RUN, "lines": list(RUN["lines"])}


def background(mode, fn):
    with LOCK:
        if RUN["active"]:
            return False
        RUN.update({"active": True, "mode": mode, "lines": [], "error": "", "finished": ""})

    def wrapper():
        error = ""
        try:
            fn()
        except Abort as exc:
            error = str(exc)
            log("ERROR: " + error)
        except Exception as exc:                      # noqa: BLE001 - surfaced in the UI
            error = "%s: %s" % (type(exc).__name__, exc)
            log("ERROR: " + error)
        with LOCK:
            RUN["active"] = False
            RUN["error"] = error
            RUN["finished"] = datetime.now().strftime("%H:%M:%S")

    threading.Thread(target=wrapper, daemon=True).start()
    return True


class LineWriter(io.TextIOBase):
    """Captures send_applications' prints into the run log, line by line."""

    def __init__(self):
        self.buf = ""

    def write(self, text):
        self.buf += text
        while "\n" in self.buf:
            line, _, self.buf = self.buf.partition("\n")
            if line.strip():
                log(line.strip())
        return len(text)


# --------------------------------------------------------------------------- #
# reading the project's state
# --------------------------------------------------------------------------- #

def read_config_raw():
    if not sa.CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(sa.CONFIG_PATH.read_text())
    except ValueError:
        return {}


def write_config_raw(cfg):
    sa.CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.chmod(sa.CONFIG_PATH, 0o600)


def config_state():
    """Never includes the app password — only whether it exists."""
    cfg = read_config_raw()
    out = {k: str(cfg.get(k) or "") for k in CONFIG_TEXT_FIELDS}
    pw = str(cfg.get("app_password") or "")
    out["has_password"] = bool(pw) and not pw.startswith("PUT_")
    missing = [k for k in ("sender_name", "sender_email") if not out[k]]
    if not out["has_password"]:
        missing.append("app_password")
    resume = sa.read_resume(sa.active_resume_name(cfg))
    if not (resume and resume["ok"]):
        missing.append("a resume (Resume tab)")
    out["missing"] = missing
    out["ready"] = not missing
    return out


def existing_header():
    if sa.JOBS_PATH.exists():
        header = []
        with sa.JOBS_PATH.open(newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if row:
                    header = [c for c in row if not c.startswith("_")]
                    break
        for k in JOB_FIELDS:
            if k not in header:
                header.append(k)
        return header
    return list(JOB_FIELDS)


def jobs_state(cfg):
    rows = sa.load_jobs() if sa.JOBS_PATH.exists() else []
    seen = sa.already_sent_addresses()
    out = []
    for row in rows:
        item = {k: (v or "") for k, v in row.items() if k and not k.startswith("_")}
        item["_skip"] = sa.check(row, seen) or sa.pick_problem(row, cfg) or ""
        out.append(item)
    return out


def sent_log_state():
    if not sa.LOG_PATH.exists():
        return []
    with sa.LOG_PATH.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def state():
    cfg = read_config_raw()
    jobs = jobs_state(cfg)
    ready = sum(1 for j in jobs if not j["_skip"])
    sent = sum(1 for j in jobs if (j.get("status") or "").lower() == "sent")
    return {
        "config": config_state(),
        "jobs": jobs,
        "header": existing_header(),
        "counts": {"total": len(jobs), "ready": ready, "blocked": len(jobs) - ready, "sent": sent},
        "templates": sa.list_templates(),
        "active_template": sa.active_template_name(cfg),
        "active_general_template": sa.active_general_name(cfg),
        "covers": sa.list_covers(),
        "active_cover": sa.active_cover_name(cfg),
        "resumes": sa.list_resumes(),
        "active_resume": sa.active_resume_name(cfg),
        "placeholders": sa.PLACEHOLDERS,
        "sent_log": sent_log_state(),
        "defaults": {"limit": 25, "delay": 45},
    }


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

def save_config(patch):
    cfg = read_config_raw()
    for key in CONFIG_TEXT_FIELDS:
        if key in patch:
            cfg[key] = str(patch[key]).strip()
    pw = "".join(str(patch.get("app_password") or "").split())   # no spaces of any kind
    if pw:                                    # blank means "keep the stored one"
        cfg["app_password"] = pw
    write_config_raw(cfg)


def save_jobs(rows):
    header = existing_header()
    clean = []
    for row in rows:
        item = {k: str(row.get(k) or "").strip() for k in header}
        if any(item.values()):                # drop rows left completely blank
            clean.append(item)
    sa.save_jobs(clean, header)
    return len(clean)


def row_key(email, company):
    return ((email or "").strip().lower(), (company or "").strip().lower())


def picked_keys(payload):
    """The rows ticked on the Queue tab, as (email, company) — the same pair the sent log
    uses to never mail anyone twice."""
    keys = {row_key(*pair) for pair in payload.get("rows") or []
            if isinstance(pair, (list, tuple)) and len(pair) == 2}
    if not keys:
        raise Abort("Tick at least one row to send.")
    return keys


def build_queue(cfg, limit, keys):
    """Mirrors the selection loop in send_applications.main(), for the ticked rows only,
    each with its own picks."""
    rows = sa.load_jobs()
    if not rows:
        raise Abort("jobs.csv has no rows yet. Add a company on the Queue tab first.")
    seen = sa.already_sent_addresses()
    queue, skipped = [], []
    for row in rows:
        if row_key(row.get("email"), row.get("company")) not in keys:
            continue
        reason = sa.check(row, seen) or sa.pick_problem(row, cfg)
        if reason:
            skipped.append([sa.label(row), reason])
            continue
        if len(queue) >= limit:
            skipped.append([sa.label(row), "over the limit of %d" % limit])
            continue
        tpl, cover, resume = sa.load_picks(row, cfg)
        queue.append((row, sa.build_message(row, cfg, tpl, resume, cover=cover)))
        seen.add((row["email"].lower().strip(), (row.get("company") or "").lower().strip()))
    return rows, queue, skipped


def do_send(payload, keys):
    cfg = sa.load_config()
    limit = max(1, int(payload.get("limit") or 25))
    delay = max(0, int(payload.get("delay") or 45))
    rows, queue, skipped = build_queue(cfg, limit, keys)
    if not queue:
        log("Nothing to send — every ticked row was skipped.")
        for company, reason in skipped:
            log("  skip  %-28s %s" % (company, reason))
        return
    log("Sending 1 email." if len(queue) == 1 else "Sending %d emails, %ds apart." % (len(queue), delay))
    for company, reason in skipped:
        log("  skip  %-28s %s" % (company, reason))
    writer = LineWriter()
    with contextlib.redirect_stdout(writer):
        results = sa.deliver(queue, cfg, delay=delay)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for row, ok in results:
        if ok:
            row["status"] = "sent"
            row["sent_at"] = stamp
    sa.save_jobs(rows, existing_header())
    good = sum(1 for _, ok in results if ok)
    log("Done. %d of %d sent. jobs.csv and sent_log.csv updated." % (good, len(results)))


# --------------------------------------------------------------------------- #
# emails and cover letters
# --------------------------------------------------------------------------- #

def doc_slug(name, path_for, fallback):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or fallback
    slug, n = base, 2
    while path_for(slug).exists():
        slug, n = "%s-%d" % (base, n), n + 1
    return slug + ".json"


def write_doc(path, data):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_template(payload):
    """Creates a new template when there is no id. Returns its id. "no_company" files it
    with the emails for rows that have no company name."""
    name = str(payload.get("name") or "").strip()
    subject = str(payload.get("subject") or "").strip()
    if not name:
        raise Abort("Give the email a name.")
    if not subject:
        raise Abort("The subject line cannot be empty.")
    tid = str(payload.get("id") or "")
    if tid and not sa.read_template(tid):
        raise Abort("That email no longer exists — reload the page.")
    tid = sa.template_path(tid).name if tid else doc_slug(name, sa.template_path, "email")
    data = {"name": name, "subject": subject,
            "greeting": str(payload.get("greeting") or "").strip(),
            "html": str(payload.get("html") or "")}
    if payload.get("no_company"):
        data["no_company"] = True
    write_doc(sa.template_path(tid), data)
    return tid


def default_key(tpl):
    return "active_general_template" if tpl["no_company"] else "active_template"


def delete_template(tid):
    tpl = sa.read_template(tid)
    if not tpl:
        raise Abort("That email no longer exists — reload the page.")
    if len(sa.list_templates()) <= 1:
        raise Abort("This is your only email. Make another one before deleting it.")
    sa.template_path(tid).unlink()
    cfg = read_config_raw()
    if cfg.get(default_key(tpl)) == tpl["id"]:       # falls back to another of that kind
        cfg.pop(default_key(tpl))
        write_config_raw(cfg)


def use_template(tid):
    """Makes it the default for its kind: rows with a company name, or rows without."""
    tpl = sa.read_template(tid)
    if not tpl:
        raise Abort("That email no longer exists — reload the page.")
    cfg = read_config_raw()
    cfg[default_key(tpl)] = tpl["id"]
    write_config_raw(cfg)


def save_cover(payload):
    """Creates a new cover letter when there is no id. Returns its id."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise Abort("Give the cover letter a name.")
    cid = str(payload.get("id") or "")
    if cid and not sa.read_cover(cid):
        raise Abort("That cover letter no longer exists — reload the page.")
    cid = sa.cover_path(cid).name if cid else doc_slug(name, sa.cover_path, "cover")
    write_doc(sa.cover_path(cid), {"name": name, "html": str(payload.get("html") or "")})
    return cid


def delete_cover(cid):
    if not sa.read_cover(cid):
        raise Abort("That cover letter no longer exists — reload the page.")
    path = sa.cover_path(cid)
    path.unlink()
    cfg = read_config_raw()
    if cfg.get("active_cover") == path.name:      # back to sending without one
        cfg.pop("active_cover")
        write_config_raw(cfg)


def use_cover(cid):
    """An empty id means send without a cover letter."""
    cfg = read_config_raw()
    if cid:
        if not sa.read_cover(cid):
            raise Abort("That cover letter no longer exists — reload the page.")
        cfg["active_cover"] = sa.cover_path(cid).name
    else:
        cfg.pop("active_cover", None)
    write_config_raw(cfg)


def preview_context(no_company=False):
    """Previews are general: your own details filled in, the company's shown as [Company].
    An email for rows with no company is previewed with no company, as it will be sent."""
    raw = read_config_raw()
    cfg = {k: str(raw.get(k) or "") for k in CONFIG_TEXT_FIELDS}
    cfg["sender_name"] = cfg["sender_name"] or "Your Name"
    cfg["sender_email"] = cfg["sender_email"] or "you@gmail.com"
    return raw, cfg, {"company": "" if no_company else "[Company]", "role": "[Role]", "email": ""}


def preview_template(payload):
    """Unsaved edits rendered exactly as build_message would render them."""
    raw, cfg, row = preview_context(bool(payload.get("no_company")))
    tpl = {k: str(payload.get(k) or "") for k in ("subject", "greeting", "html")}
    body = sa.render_body(tpl, row, cfg)
    subject = sa.render_subject(tpl, row, cfg)
    text = sa.html_to_text(body)
    resume = sa.read_resume(sa.active_resume_name(raw))
    attachments = [resume["filename"]] if resume else []
    if sa.active_cover_name(raw):
        attachments.append(sa.cover_filename(cfg))
    return {
        "from": "%s <%s>" % (cfg["sender_name"], cfg["sender_email"]),
        "to": "[the company's email address]",
        "subject": subject,
        "html": sa.email_html(body),
        "text": text,
        "attachments": attachments,
        "problem": sa.template_problem(subject, text) or "",
    }


def preview_cover(payload):
    _raw, cfg, row = preview_context()
    body = sa.render_cover({"html": str(payload.get("html") or "")}, row, cfg)
    return {"html": body, "filename": sa.cover_filename(cfg),
            "problem": sa.template_problem(sa.html_to_text(body)) or ""}


def preview_cover_pdf(payload):
    _raw, cfg, row = preview_context()
    body = sa.render_cover({"html": str(payload.get("html") or "")}, row, cfg)
    return cover_pdf.render(body, title="Cover letter")


# --------------------------------------------------------------------------- #
# resumes
# --------------------------------------------------------------------------- #

def save_resume(payload):
    """Creates a new resume when there is no id. An upload ("data", base64) is copied
    into resumes/files/; otherwise "path" points at a PDF already on this Mac."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise Abort("Give the resume a name.")
    rid = str(payload.get("id") or "")
    if rid and not sa.read_resume(rid):
        raise Abort("That resume no longer exists — reload the page.")
    rid = sa.resume_doc(rid).name if rid else doc_slug(name, sa.resume_doc, "resume")
    path = os.path.expanduser(str(payload.get("path") or "").strip())
    if payload.get("data"):
        try:
            blob = base64.b64decode(str(payload["data"]).split(",", 1)[-1], validate=True)
        except ValueError:
            raise Abort("That upload could not be read — try again.")
        if not blob.startswith(b"%PDF"):
            raise Abort("That file is not a PDF.")
        dest = sa.RESUME_DIR / "files" / (Path(rid).stem + ".pdf")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        path = str(dest)
    if not path:
        raise Abort("Upload a PDF, or give the path to one on this Mac.")
    if not Path(path).is_file():
        raise Abort("There is no file at " + path)
    filename = str(payload.get("filename") or "").strip() or os.path.basename(path)
    write_doc(sa.resume_doc(rid), {"name": name, "path": path, "filename": filename})
    return rid


def delete_resume(rid):
    res = sa.read_resume(rid)
    if not res:
        raise Abort("That resume no longer exists — reload the page.")
    if len(sa.list_resumes()) <= 1:
        raise Abort("This is your only resume. Add another one before deleting it.")
    sa.resume_doc(rid).unlink()
    uploads = (sa.RESUME_DIR / "files").resolve()
    if res["file"].is_file() and res["file"].resolve().parent == uploads:
        res["file"].unlink()                      # only copies this app made
    cfg = read_config_raw()
    if cfg.get("active_resume") == res["id"]:
        cfg.pop("active_resume")
        write_config_raw(cfg)


def use_resume(rid):
    if not sa.read_resume(rid):
        raise Abort("That resume no longer exists — reload the page.")
    cfg = read_config_raw()
    cfg["active_resume"] = sa.resume_doc(rid).name
    write_config_raw(cfg)


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "JobMailerUI"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, code=200, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj), code)

    def _local(self):
        return self.client_address[0] in LOCAL_HOSTS

    def _authed(self):
        # A cross-origin page cannot set this header without a preflight we never answer.
        token = self.headers.get("X-Token") or ""
        return bool(token) and token == TOKEN and self._local()

    def _payload(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path == "/":
            boot = TOKEN if (self._local() and query.get("t", [""])[0] == TOKEN) else ""
            if not boot:
                return self._send(
                    "<h1>Open the link printed in your terminal</h1>"
                    "<p>It is also saved in <code>.ui_url</code> next to web_ui.py.</p>",
                    403, "text/html; charset=utf-8")
            html = INDEX.read_text(encoding="utf-8").replace("__TOKEN__", boot)
            return self._send(html, 200, "text/html; charset=utf-8")

        if not url.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if not self._authed():
            return self._json({"error": "not logged in"}, 403)

        if url.path == "/api/state":
            return self._json(state())
        if url.path == "/api/run":
            return self._json(run_snapshot())
        if url.path == "/api/template":
            tpl = sa.read_template(query.get("name", [""])[0])
            if not tpl:
                return self._json({"error": "no such template"}, 404)
            return self._json(tpl)
        if url.path == "/api/cover":
            cover = sa.read_cover(query.get("name", [""])[0])
            if not cover:
                return self._json({"error": "no such cover letter"}, 404)
            return self._json(cover)
        if url.path == "/api/resume/file":
            res = sa.read_resume(query.get("name", [""])[0])
            if not res or not res["ok"]:
                return self._json({"error": "no such resume file"}, 404)
            return self._send(res["file"].read_bytes(), 200, "application/pdf")
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        try:
            payload = self._payload()
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if not self._authed():
            return self._json({"error": "not logged in"}, 403)

        try:
            if url.path == "/api/config":
                save_config(payload)
                return self._json({"ok": True, "config": config_state()})
            if url.path == "/api/jobs":
                return self._json({"ok": True, "saved": save_jobs(payload.get("rows") or [])})
            if url.path == "/api/template":
                return self._json({"ok": True, "id": save_template(payload)})
            if url.path == "/api/template/delete":
                delete_template(payload.get("id"))
                return self._json({"ok": True})
            if url.path == "/api/template/use":
                use_template(payload.get("id"))
                return self._json({"ok": True})
            if url.path == "/api/template/preview":
                return self._json(preview_template(payload))
            if url.path == "/api/cover":
                return self._json({"ok": True, "id": save_cover(payload)})
            if url.path == "/api/cover/delete":
                delete_cover(payload.get("id"))
                return self._json({"ok": True})
            if url.path == "/api/cover/use":
                use_cover(payload.get("id") or "")
                return self._json({"ok": True})
            if url.path == "/api/cover/preview":
                return self._json(preview_cover(payload))
            if url.path == "/api/cover/pdf":
                return self._send(preview_cover_pdf(payload), 200, "application/pdf")
            if url.path == "/api/resume":
                return self._json({"ok": True, "id": save_resume(payload)})
            if url.path == "/api/resume/delete":
                delete_resume(payload.get("id"))
                return self._json({"ok": True})
            if url.path == "/api/resume/use":
                use_resume(payload.get("id"))
                return self._json({"ok": True})
            if url.path == "/api/send":
                keys = picked_keys(payload)
                started = background("send", lambda: do_send(payload, keys))
                return self._json({"ok": started,
                                   "error": "" if started else "a run is already going"})
        except Abort as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:                      # noqa: BLE001 - surfaced in the UI
            return self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
        return self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="Local web UI for the job application mailer.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = ap.parse_args()
    sa.adopt_config_resume(read_config_raw())

    httpd = None
    for port in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("No free port in %d-%d." % (args.port, args.port + 19))

    url = "http://127.0.0.1:%d/?t=%s" % (port, TOKEN)
    URL_FILE.write_text(url + "\n", encoding="utf-8")
    os.chmod(URL_FILE, 0o600)

    print("\n  Job Application Mailer — web UI")
    print("  %s\n" % url)
    print("  Listening on   : 127.0.0.1:%d (this Mac only)" % port)
    print("\n  Ctrl-C to stop.\n", flush=True)

    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
