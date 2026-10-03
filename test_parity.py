#!/usr/bin/env python3
"""
Proves the online version (api/_mailcore.py, api/_cover_pdf.py) renders exactly what the
local one (send_applications.py, cover_pdf.py) does.   python3 test_parity.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "api"))
import _cover_pdf as online_pdf
import _mailcore as online
import cover_pdf as local_pdf
import send_applications as local

cfg = {"sender_name": "Mohammad Yousaf Hasan", "sender_email": "me@example.com", "phone": "+92 300 0000000",
       "linkedin": "linkedin.com/in/x", "github": "github.com/x", "portfolio": ""}
rows = [{"company": "Acme & Sons", "role": "AI Engineer", "contact_name": "Sara", "email": "a@acme.com"},
        {"company": "", "role": "", "email": "b@b.com"},
        {"company": "Globex <Ltd>", "role": "ML Engineer", "location": "Lahore", "email": "c@g.com"}]
now = datetime(2026, 10, 3, 9, 30)
docs = [json.loads(p.read_text()) for p in sorted((ROOT / "templates").glob("*.json"))]
covers = [json.loads(p.read_text()) for p in sorted((ROOT / "covers").glob("*.json"))]
bad = 0
def same(label, a, b):
    global bad
    ok = a == b
    bad += not ok
    print(("ok   " if ok else "DIFF ") + label)

# the local code asks the clock for the date; pin it to the same moment
class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return now
local.datetime = Frozen
for tpl in docs:
    for row in rows:
        same("email  %-28s %-14s" % (tpl["name"], row["company"][:14] or "(no company)"),
             (local.render_subject(tpl, row, cfg), local.render_body(tpl, row, cfg),
              local.html_to_text(local.render_body(tpl, row, cfg))),
             (online.render_subject(tpl, row, cfg, now), online.render_body(tpl, row, cfg, now),
              online.html_to_text(online.render_body(tpl, row, cfg, now))))
for cv in covers:
    for row in rows:
        a, b = local.render_cover(cv, row, cfg), online.render_cover(cv, row, cfg, now)
        same("cover  %-28s %-14s" % (cv["name"], row["company"][:14] or "(no company)"), a, b)
        same("  its PDF is byte-identical", local_pdf.render(a, "t"), online_pdf.render(b, "t"))
for row in rows + [{"company": "X", "email": "bad"}, {"company": "X", "email": "x@y.com", "status": "hold"},
                   {"company": "X", "email": "x@y.com", "status": "sent"}]:
    seen = {("b@b.com", "")}
    same("check  %-40s" % json.dumps(row)[:40], local.check(row, seen), online.check(row, seen))
same("web/index.html == public/index.html (the page Vercel serves)", (ROOT / "web" / "index.html").read_bytes(),
     (ROOT / "public" / "index.html").read_bytes())
same("cover_pdf.py == api/_cover_pdf.py (same file)", (ROOT / "cover_pdf.py").read_bytes(),
     (ROOT / "api" / "_cover_pdf.py").read_bytes())
print("\nALL IDENTICAL" if not bad else "\n%d DIFFERENCES" % bad)
sys.exit(1 if bad else 0)
