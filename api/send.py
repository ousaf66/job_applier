import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _core import (Abort, append_log, build_message, deliver, endpoint, eligible,
                   get_config, get_jobs, get_template_text, kv_set_json, local_stamp,
                   split_template)


def send_one(payload):
    """Sends exactly ONE email and returns. The browser calls this repeatedly with
    the delay in between, because a serverless function cannot sit and sleep."""
    cfg = get_config()
    subject_tpl, body_tpl = split_template(get_template_text(payload.get("template")
                                                             or "application.txt"))
    only = (payload.get("only") or "").strip()
    stamp = local_stamp(payload.get("tz") or 0)

    if payload.get("test"):
        rows = get_jobs()
        sample = dict(rows[0]) if rows else {"company": "Test Company", "role": "AI Engineer"}
        sample.setdefault("company", "Test Company")
        msg = build_message(sample, cfg, subject_tpl, body_tpl, to_addr=cfg["sender_email"])
        deliver(msg, cfg)
        return {"ok": True, "test": True, "to": cfg["sender_email"], "done": True, "remaining": 0}

    rows = get_jobs()
    picked = eligible(rows, only)
    if not picked:
        return {"ok": True, "done": True, "remaining": 0, "company": "", "result": "nothing to send"}

    index = picked[0]
    row = rows[index]
    msg = build_message(row, cfg, subject_tpl, body_tpl)

    try:
        deliver(msg, cfg)
        result = "sent"
    except Abort as exc:
        result = "failed: %s" % exc

    append_log({
        "sent_at": stamp, "company": row.get("company", ""), "role": row.get("role", ""),
        "email": msg["To"], "subject": msg["Subject"], "source": row.get("source", ""),
        "job_url": row.get("job_url", ""), "result": result,
    })
    # Either way the row is parked, so the next call moves on instead of retrying forever.
    row["status"] = "sent" if result == "sent" else "failed"
    row["sent_at"] = stamp
    rows[index] = row
    kv_set_json("jobs", rows)

    remaining = len(eligible(rows, only))
    return {"ok": result == "sent", "company": row.get("company", ""), "to": msg["To"],
            "result": result, "remaining": remaining, "done": remaining == 0}


handler = endpoint(send_one)
