# Deploying to Vercel

The online version is the same app as the one on your Mac — Queue with tick boxes, Email,
Cover letter, Resume, Sent log, Setup — and every email goes out with your **resume and
cover letter attached as PDFs**. It runs as one Python function (`api/app.py`) plus the
page in `public/`, with an Upstash Redis database for your data. Nothing else.

---

## 1. Get the code to GitHub

The repo already points at `https://github.com/ousaf66/job_applier.git`.

```bash
cd ~/personal_projects/job_apply_bot
git add -A
git commit -m "Online version: covers, resumes, per-row picks, select-to-send"
git push -u origin main
```

Keep the repo **private** — `jobs.csv` and the seed files hold your queue and details.
`config.json` (your Gmail app password) is excluded by `.gitignore` and `.vercelignore`
and never leaves this Mac.

## 2. Import it into Vercel

vercel.com → **Add New → Project → Import** `job_applier`. Take every default and deploy.
The first deploy shows an error page — expected, until steps 3 and 4 are done.

## 3. Add the database

In the project: **Storage → Create Database → Upstash for Redis → Connect**.
That injects `KV_REST_API_URL` and `KV_REST_API_TOKEN` automatically.

## 4. Add three environment variables

**Settings → Environment Variables**, all environments:

| Name | Value |
|---|---|
| `GMAIL_ADDRESS` | `yousaf.hasan66@gmail.com` |
| `GMAIL_APP_PASSWORD` | the 16-letter Google App Password (spaces don't matter) |
| `SESSION_SECRET` | any long random string |

Then **Deployments → ⋯ → Redeploy**. Variables only apply to builds made after them.

## 5. First run — in this order

There is no login: **anyone who has your `*.vercel.app` address can use the page** — send email
from your Gmail, and see your resume, phone number and queue. Keep the address to yourself, or
lock it (next paragraph). To open it, just go to the address.

**To lock it again** later, add an environment variable `UI_PASSWORD` (a long key, letters and
digits only) and redeploy. The page then shows "This page is locked" until you open your private
link **once on each device**:

```
https://<your-project>.vercel.app/?k=<UI_PASSWORD>
```

The page keeps the key in that browser and removes it from the address bar, so from then on the
plain `https://<your-project>.vercel.app` opens straight away. Anyone without the key sees only
"This page is private" — the address alone can't send email as you. Don't share the link.

1. **Setup** — type your name, phone, LinkedIn, GitHub. Save. (The Gmail address and app
   password show as "set in Vercel" — they come from step 4.)
2. **Resume** — **+ Add resume**, pick your PDF (up to 700 KB), Save. It becomes the Default.
3. **Cover letter** — open **Standard cover letter**, check the preview, press **Make
   default** so it's attached to every email. (Skip this to send without one.)
4. **Email** — your three emails are already there: two for rows *with* a company name, one
   for rows *without*. Edit them if you like.
5. **Queue** — your queue is already there. **Send yourself a test first:** add a row with
   your own Gmail address, tick only that row, press **Send 1 selected**, and check the
   email arrives with both PDFs and that they open.
6. Then tick the real rows and send.

## How sending works here

- **Keep the tab open while sending.** A serverless function can't sleep 45 seconds between
  emails, so the page sends one email per request and does the waiting itself. Close the
  tab and it stops where it is; sent rows stay marked **sent**, the rest stay ticked-able.
  **Stop after this email** is there if you change your mind.
- A failed send leaves the row unsent, records the reason in the Sent log, and stops the
  batch so you can read it.
- The same address + company is never mailed twice.

## Your data

Everything you change online — queue, emails, covers, resumes, settings, sent log — lives in
Redis, **not** in the repo. The files in `api/assets/` (`jobs.seed.csv`, `templates/`,
`covers/`) are only the starting data, copied in on the very first request. Changing them
later does nothing to an app that's already running. The local app and the online app have
**separate** data; they don't sync.

## Changing the code later

`public/index.html` is a copy of `web/index.html`, and `api/_cover_pdf.py` a copy of
`cover_pdf.py`. After editing either original:

```bash
cp web/index.html public/index.html
cp cover_pdf.py api/_cover_pdf.py
python3 test_parity.py        # must end with ALL IDENTICAL
```

`test_parity.py` also proves the online emails render exactly like the local ones.

## If sending fails with a connection error

Gmail SMTP on port 465 normally works from Vercel, but this is the one thing that can't be
checked without deploying. If every send times out, the platform is blocking outbound SMTP.
The code has a fallback that sends over HTTPS instead (with both attachments) — add two
environment variables and redeploy:

| Name | Value |
|---|---|
| `RESEND_API_KEY` | an API key from resend.com |
| `RESEND_FROM` | `you@yourdomain.com` |

Resend won't send *as* a gmail.com address — you need a domain you own and have verified
with them. Applications then arrive from that domain, with your Gmail as the reply-to.

## Limits worth knowing

- Resume PDFs are capped at 700 KB each (Upstash's request limit). Yours is about 146 KB.
- Hobby-plan functions run up to 60 seconds; one email takes a few seconds.
- Gmail allows roughly 500 messages a day; 25–30, spaced out, is the safe pace. The page
  sends at most 25 per press.
- Upstash's free tier is far more than this app will ever use.
