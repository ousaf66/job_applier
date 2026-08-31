#!/usr/bin/env python3
"""
Local web UI for the job application mailer.

    python3 web_ui.py                    # localhost only, link printed below
    python3 web_ui.py --set-password     # set the login password
    python3 web_ui.py --host 0.0.0.0     # reachable from other machines (needs a password)

Everything the CLI does — edit the queue, dry-run previews, test send, real send —
with buttons instead of flags. Standard library only.

All the actual work is still done by send_applications.py. This is a front end,
not a second implementation.

SECURITY: this page can send email as you and holds a Gmail app password. It binds
to 127.0.0.1 unless you say otherwise, and refuses to listen on any other address
until a login password is set. Plain HTTP is not encrypted — if you want to reach
it from outside this Mac, put it behind a tunnel that terminates TLS (Tailscale,
Cloudflare Tunnel, ngrok) rather than forwarding a port on your router.
"""

import argparse
import base64
import contextlib
import csv
import getpass
import hashlib
import hmac
import io
import json
import os
import secrets
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import send_applications as sa

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "web" / "index.html"
URL_FILE = ROOT / ".ui_url"
TOKEN = secrets.token_urlsafe(16)

CONFIG_TEXT_FIELDS = ["sender_name", "sender_email", "reply_to", "resume_path",
                      "resume_filename", "phone", "linkedin", "github", "portfolio"]
JOB_FIELDS = ["company", "role", "contact_name", "email", "source", "job_url",
              "location", "notes", "status", "sent_at"]
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


class Abort(Exception):
    """send_applications.die() raises this instead of killing the server."""


def _die(msg):
    raise Abort(msg)


sa.die = _die


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #

SESSIONS = {}                  # token -> expiry timestamp
SESSION_TTL = 12 * 3600
FAILS = {}                     # ip -> [count, locked_until]
AUTH_LOCK = threading.Lock()


def hash_password(password, rounds=240000):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "pbkdf2$%d$%s$%s" % (rounds, base64.b64encode(salt).decode(),
                                base64.b64encode(digest).decode())


def verify_password(password, stored):
    try:
        scheme, rounds, salt_b64, digest_b64 = str(stored).split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def password_is_set():
    return bool(read_config_raw().get("ui_password_hash"))


def new_session():
    now = time.time()
    with AUTH_LOCK:
        for tok, exp in list(SESSIONS.items()):
            if exp <= now:
                del SESSIONS[tok]
        token = secrets.token_urlsafe(24)
        SESSIONS[token] = now + SESSION_TTL
    return token


def valid_session(token):
    with AUTH_LOCK:
        expiry = SESSIONS.get(token)
        if expiry and expiry > time.time():
            return True
        SESSIONS.pop(token, None)
    return False


def login_attempt(ip, password):
    """Returns (token, error, retry_after)."""
    with AUTH_LOCK:
        count, until = FAILS.get(ip, [0, 0])
    if until > time.time():
        return None, "too many attempts", int(until - time.time())
    stored = read_config_raw().get("ui_password_hash")
    if not stored:
        return None, "no login password is set — set one on the Setup tab from the Mac itself", 0
    if verify_password(password or "", stored):
        with AUTH_LOCK:
            FAILS.pop(ip, None)
        return new_session(), "", 0
    with AUTH_LOCK:
        count += 1
        wait = 0 if count < 3 else min(300, 5 * (2 ** (count - 3)))
        FAILS[ip] = [count, time.time() + wait]
    return None, "wrong password", wait


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
    """Never includes the app password or the login hash — only whether they exist."""
    cfg = read_config_raw()
    out = {k: str(cfg.get(k) or "") for k in CONFIG_TEXT_FIELDS}
    pw = str(cfg.get("app_password") or "")
    out["has_password"] = bool(pw) and not pw.startswith("PUT_")
    out["has_ui_password"] = bool(cfg.get("ui_password_hash"))
    resume = Path(os.path.expanduser(out["resume_path"])) if out["resume_path"] else None
    out["resume_ok"] = bool(resume and resume.exists())
    out["resume_full"] = str(resume) if resume else ""
    missing = [k for k in ("sender_name", "sender_email", "resume_path") if not out[k]]
    if not out["has_password"]:
        missing.append("app_password")
    out["missing"] = missing
    out["ready"] = not missing and out["resume_ok"]
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


def jobs_state():
    rows = sa.load_jobs() if sa.JOBS_PATH.exists() else []
    seen = sa.already_sent_addresses()
    out = []
    for row in rows:
        item = {k: (v or "") for k, v in row.items() if not k.startswith("_")}
        item["_skip"] = sa.check(row, seen) or ""
        out.append(item)
    return out


def sent_log_state():
    if not sa.LOG_PATH.exists():
        return []
    with sa.LOG_PATH.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def state():
    jobs = jobs_state()
    ready = sum(1 for j in jobs if not j["_skip"])
    sent = sum(1 for j in jobs if (j.get("status") or "").lower() == "sent")
    return {
        "config": config_state(),
        "jobs": jobs,
        "header": existing_header(),
        "counts": {"total": len(jobs), "ready": ready, "blocked": len(jobs) - ready, "sent": sent},
        "templates": sorted(p.name for p in (ROOT / "templates").glob("*.txt")),
        "previews": sorted(p.name for p in sa.PREVIEW_DIR.glob("*.txt")),
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
    pw = str(patch.get("app_password") or "").strip()
    if pw:                                    # blank means "keep the stored one"
        cfg["app_password"] = pw
    ui_pw = str(patch.get("ui_password") or "").strip()
    if ui_pw:
        if len(ui_pw) < 8:
            raise Abort("Login password must be at least 8 characters.")
        cfg["ui_password_hash"] = hash_password(ui_pw)
    if patch.get("ui_password_clear"):
        cfg.pop("ui_password_hash", None)
        with AUTH_LOCK:
            SESSIONS.clear()
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


def build_queue(cfg, subject_tpl, body_tpl, limit, only):
    """Mirrors the selection loop in send_applications.main()."""
    rows = sa.load_jobs()
    if not rows:
        raise Abort("jobs.csv has no rows yet. Add a company on the Queue tab first.")
    seen = sa.already_sent_addresses()
    queue, skipped = [], []
    for row in rows:
        if only and only.lower() not in (row.get("company") or "").lower():
            continue
        reason = sa.check(row, seen)
        if reason:
            skipped.append([row.get("company") or "?", reason])
            continue
        if len(queue) >= limit:
            skipped.append([row.get("company") or "?", "over the limit of %d" % limit])
            continue
        row["_attachment"] = cfg["resume_path"].name
        queue.append((row, sa.build_message(row, cfg, subject_tpl, body_tpl)))
        seen.add((row["email"].lower().strip(), (row.get("company") or "").lower().strip()))
    return rows, queue, skipped


def prepare(payload):
    cfg = sa.load_config()
    subject_tpl, body_tpl = sa.load_template(payload.get("template") or "application.txt")
    limit = max(1, int(payload.get("limit") or 25))
    delay = max(0, int(payload.get("delay") or 45))
    only = (payload.get("only") or "").strip()
    return cfg, subject_tpl, body_tpl, limit, delay, only


def do_dryrun(payload):
    cfg, subject_tpl, body_tpl, limit, _delay, only = prepare(payload)
    _rows, queue, skipped = build_queue(cfg, subject_tpl, body_tpl, limit, only)
    for stale in sa.PREVIEW_DIR.glob("*.txt"):    # a preview folder = exactly one run
        stale.unlink()
    written = [sa.write_preview(i, row, msg).name for i, (row, msg) in enumerate(queue, start=1)]
    return {"written": written, "skipped": skipped, "count": len(queue)}


def do_test(payload):
    cfg, subject_tpl, body_tpl, _limit, _delay, _only = prepare(payload)
    rows = sa.load_jobs()
    sample = dict(rows[0]) if rows else {"company": "Test Company", "role": "AI Engineer"}
    sample.setdefault("company", "Test Company")
    sample["_attachment"] = cfg["resume_path"].name
    msg = sa.build_message(sample, cfg, subject_tpl, body_tpl, to_addr=cfg["sender_email"])
    log("Sending one test email to %s ..." % cfg["sender_email"])
    writer = LineWriter()
    with contextlib.redirect_stdout(writer):
        sa.deliver([(sample, msg)], cfg, delay=0)
    log("Done. Check your inbox and open the attachment.")


def do_send(payload):
    cfg, subject_tpl, body_tpl, limit, delay, only = prepare(payload)
    rows, queue, skipped = build_queue(cfg, subject_tpl, body_tpl, limit, only)
    if not queue:
        log("Nothing to send — every row was skipped.")
        return
    log("Sending %d email%s, %ds apart." % (len(queue), "" if len(queue) == 1 else "s", delay))
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
        if token and valid_session(token):
            return True
        return bool(token) and token == TOKEN and self._local()

    def _payload(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path == "/":
            boot = TOKEN if (self._local() and query.get("t", [""])[0] == TOKEN) else ""
            if not boot and not password_is_set():
                return self._send(
                    "<h1>Open the link printed in your terminal</h1>"
                    "<p>Or set a login password so you can sign in from anywhere:<br>"
                    "<code>python3 web_ui.py --set-password</code></p>",
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
        if url.path == "/api/preview":
            name = os.path.basename(query.get("name", [""])[0])
            path = sa.PREVIEW_DIR / name
            if not name or not path.exists():
                return self._json({"error": "no such preview"}, 404)
            return self._json({"name": name, "text": path.read_text(encoding="utf-8")})
        if url.path == "/api/template":
            name = os.path.basename(query.get("name", [""])[0])
            path = ROOT / "templates" / name
            if not name or not path.exists():
                return self._json({"error": "no such template"}, 404)
            return self._json({"name": name, "text": path.read_text(encoding="utf-8")})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        try:
            payload = self._payload()
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if url.path == "/api/login":
            token, error, wait = login_attempt(self.client_address[0], payload.get("password"))
            if token:
                return self._json({"token": token})
            msg = error if not wait else "%s — try again in %ds" % (error, wait)
            return self._json({"error": msg}, 403)

        if not self._authed():
            return self._json({"error": "not logged in"}, 403)

        if url.path == "/api/logout":
            with AUTH_LOCK:
                SESSIONS.pop(self.headers.get("X-Token") or "", None)
            return self._json({"ok": True})

        try:
            if url.path == "/api/config":
                save_config(payload)
                return self._json({"ok": True, "config": config_state()})
            if url.path == "/api/jobs":
                return self._json({"ok": True, "saved": save_jobs(payload.get("rows") or [])})
            if url.path == "/api/template":
                name = os.path.basename(payload.get("name") or "")
                path = ROOT / "templates" / name
                if not name or not path.exists():
                    return self._json({"error": "no such template"}, 404)
                path.write_text(payload.get("text") or "", encoding="utf-8")
                return self._json({"ok": True})
            if url.path == "/api/dryrun":
                return self._json({"ok": True, **do_dryrun(payload)})
            if url.path == "/api/test":
                started = background("test", lambda: do_test(payload))
                return self._json({"ok": started,
                                   "error": "" if started else "a run is already going"})
            if url.path == "/api/send":
                started = background("send", lambda: do_send(payload))
                return self._json({"ok": started,
                                   "error": "" if started else "a run is already going"})
        except Abort as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:                      # noqa: BLE001 - surfaced in the UI
            return self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
        return self._json({"error": "not found"}, 404)


def set_password_interactive():
    first = getpass.getpass("New login password (min 8 chars): ")
    if len(first) < 8:
        raise SystemExit("Too short — nothing changed.")
    if first != getpass.getpass("Again: "):
        raise SystemExit("They did not match — nothing changed.")
    cfg = read_config_raw()
    cfg["ui_password_hash"] = hash_password(first)
    write_config_raw(cfg)
    print("\n  Login password saved to config.json (stored hashed, not in plain text).\n")


def main():
    ap = argparse.ArgumentParser(description="Local web UI for the job application mailer.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; anything but localhost requires a login password")
    ap.add_argument("--set-password", action="store_true", help="set the login password and exit")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = ap.parse_args()

    if args.set_password:
        return set_password_interactive()

    remote = args.host not in LOCAL_HOSTS
    if remote and not password_is_set():
        raise SystemExit(
            "\n  Refusing to listen on %s with no login password.\n"
            "  This page can send email as you and holds your Gmail app password.\n\n"
            "      python3 web_ui.py --set-password\n" % args.host)

    httpd = None
    for port in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("No free port in %d-%d." % (args.port, args.port + 19))

    url = "http://%s:%d/?t=%s" % ("127.0.0.1" if not remote else args.host, port, TOKEN)
    URL_FILE.write_text(url + "\n", encoding="utf-8")
    os.chmod(URL_FILE, 0o600)

    print("\n  Job Application Mailer — web UI")
    print("  %s\n" % url)
    print("  Login password : %s" % ("set" if password_is_set() else "not set (localhost only)"))
    print("  Listening on   : %s:%d" % (args.host, port))
    if remote:
        print("\n  WARNING: plain HTTP is not encrypted. Reach this through a tunnel that")
        print("  terminates TLS (Tailscale, Cloudflare Tunnel, ngrok) — do not forward a")
        print("  router port straight at it.")
    print("\n  Ctrl-C to stop.\n", flush=True)

    if not args.no_open and not remote:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
