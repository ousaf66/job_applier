"""
The one serverless function behind the whole online app.

The page calls /api/app?r=<route> (for example /api/app?r=template/preview), so there is
no URL rewriting to get wrong. Every route returns JSON, except the two PDF ones. There is no login.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _core as core
from _core import Abort

# (method, route) -> function(payload). A function returns a dict (sent as JSON) or a
# (bytes, content-type) pair (sent as a file).
ROUTES = {
    ("GET", "state"): core.state,
    ("POST", "config"): core.save_settings,
    ("POST", "jobs"): core.save_jobs,
    ("POST", "send"): core.send_one,
    ("GET", "template"): core.get_template,
    ("POST", "template"): core.save_template,
    ("POST", "template/delete"): core.delete_template,
    ("POST", "template/use"): core.use_template,
    ("POST", "template/preview"): core.preview_template,
    ("GET", "cover"): core.get_cover,
    ("POST", "cover"): core.save_cover,
    ("POST", "cover/delete"): core.delete_cover,
    ("POST", "cover/use"): core.use_cover,
    ("POST", "cover/preview"): core.preview_cover,
    ("POST", "cover/pdf"): core.preview_cover_pdf,
    ("POST", "resume"): core.save_resume,
    ("POST", "resume/delete"): core.delete_resume,
    ("POST", "resume/use"): core.use_resume,
    ("GET", "resume/file"): core.resume_file,
}


class handler(BaseHTTPRequestHandler):
    def _reply(self, body, code=200, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        route = query.pop("r", "").strip("/")
        fn = ROUTES.get((self.command, route))
        if fn is None:
            return self._reply({"error": "not found"}, 404)
        try:
            if self.command == "GET":
                payload = query
            else:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            result = fn(payload)
            if isinstance(result, tuple):
                return self._reply(result[0], 200, result[1])
            self._reply(result)
        except Abort as exc:
            self._reply({"error": str(exc)}, 400)
        except Exception as exc:                  # noqa: BLE001 - surfaced in the UI
            self._reply({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    do_GET = _dispatch
    do_POST = _dispatch

    def log_message(self, fmt, *args):
        pass
