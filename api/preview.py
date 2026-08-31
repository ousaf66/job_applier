import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _core import (build_message, check, endpoint, eligible, get_config, get_jobs,
                   get_template_text, sent_pairs, split_template)


def preview(payload):
    """Renders what would go out. Sends nothing, stores nothing."""
    cfg = get_config()
    subject_tpl, body_tpl = split_template(get_template_text(payload.get("template")
                                                             or "application.txt"))
    only = (payload.get("only") or "").strip()
    limit = max(1, int(payload.get("limit") or 25))
    rows = get_jobs()
    picked = eligible(rows, only)[:limit]

    items = []
    for index in picked:
        row = rows[index]
        msg = build_message(row, cfg, subject_tpl, body_tpl)
        attachment = ""
        for part in msg.iter_attachments():          # get_filename() is None on the container
            attachment = part.get_filename() or ""
            break
        items.append({
            "company": row.get("company", ""),
            "to": msg["To"],
            "subject": msg["Subject"],
            "body": msg.get_body(preferencelist=("plain",)).get_content(),
            "attachment": attachment,
        })

    seen = sent_pairs()
    skipped = []
    for row in rows:
        if only and only.lower() not in (row.get("company") or "").lower():
            continue
        reason = check(row, seen)
        if reason:
            skipped.append([row.get("company") or "?", reason])
    over = len(eligible(rows, only)) - len(picked)
    if over > 0:
        skipped.append(["%d more" % over, "over the limit of %d" % limit])
    return {"count": len(items), "items": items, "skipped": skipped}


handler = endpoint(preview)
