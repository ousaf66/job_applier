# Job Application Mailer

Sends a personalised application email, with your resume attached, to every company
listed in `jobs.csv`. Runs entirely on this Mac through Gmail SMTP.

---

## 1. One-time setup

### a) Create a Google App Password

A normal Gmail password will not work — Google blocks it for SMTP.

1. Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/two-step-verification
2. Go to https://myaccount.google.com/apppasswords
3. Name it `job mailer`, click Create, copy the 16-character code.

### b) Fill in `config.json`

```json
{
  "sender_name":  "Mohammad Yousaf Hasan",
  "sender_email": "yousaf.hasan66@gmail.com",   <- the Gmail the app password belongs to
  "app_password": "abcd efgh ijkl mnop",        <- paste it here (spaces are fine)
  "resume_path":  "/Users/yousafhasan/Documents/Resume/Mohammad_Yousaf_Hasan_Resume.pdf"
}
```

`config.json` holds a live credential. It is already in `.gitignore` and set to
permissions 600 — never commit it or paste it anywhere.

---

## 2. Add jobs

Open `jobs.csv` in Numbers/Excel and add one row per application:

| column | meaning |
|---|---|
| `company` | required — company name, used in the email body |
| `role` | required — exact job title from the posting |
| `contact_name` | optional — recruiter's name. Blank becomes "Hi Hiring Team," |
| `email` | **required** — where it goes. No email, no send. |
| `source` | LinkedIn / Indeed / Rozee.pk — just for your records |
| `job_url` | link to the posting — just for your records |
| `location` | for your records |
| `notes` | for your records |
| `status` | leave blank. `sent` after sending; set to `skip` to exclude a row |
| `sent_at` | filled in automatically |

Delete the `Example Corp` row once you've seen the shape.

---

## 3. Run it

```bash
cd ~/personal_projects/job_apply_bot

python3 send_applications.py                  # DRY RUN — writes previews/, sends nothing
python3 send_applications.py --test           # sends ONE email to yourself
python3 send_applications.py --send           # sends for real
```

Useful flags:

```bash
--limit 10          # cap this run (default 25)
--delay 60          # seconds between emails (default 45)
--template short.json  # use the shorter email
--cover none        # leave the cover letter off this run
--resume main       # send this resume to every row this run
--only Systems      # only companies matching this text
```

Each row in `jobs.csv` can pick its own email, cover letter and resume in the
`template`, `cover` and `resume` columns (blank = the default; `none` in `cover` = no
cover letter). Rows with a company name default to one email and rows without one to
another: emails marked `"no_company": true` are for rows with no company. The `--template`,
`--cover` and `--resume` flags override every row for one run.

**Do the `--test` run once before your first real batch** — it proves the login works
and lets you confirm the resume attachment opens properly on the other end.

---

## 4. What stops you shooting yourself in the foot

- Nothing sends without `--send`.
- A row already marked `sent` is never sent again.
- `sent_log.csv` records every send; the same address+company is never mailed twice
  even if you duplicate a row.
- Malformed or missing email addresses are skipped and reported, not sent.
- 45 seconds between sends and a 25-per-run cap keep Gmail from flagging the account.

---

## 5. Things worth knowing

- **Gmail limits.** A free account allows roughly 500 messages a day, but sending
  dozens of near-identical mails in a short window is what gets accounts throttled.
  25–30 a day, spaced out, is a safe pace.
- **LinkedIn does not publish employer emails.** Job posts have an Apply button, not an
  address, and scraping LinkedIn breaks their terms and risks your account. This tool is
  for the cases where you *have* an address: careers@ / hr@ / jobs@ pages, recruiters who
  posted their email in the listing, people you found on the company site, referrals.
  For the rest, apply through the LinkedIn/Indeed form itself.
- **Cold email works better with a real person's name** in `contact_name` and a specific
  role title. Blank contacts still send, they just convert worse.

---

## Files

```
send_applications.py    the tool
web_ui.py               the same thing in a browser: python3 web_ui.py
api/ + public/         the online (Vercel) version of the same app — see DEPLOY.md
test_parity.py          proves online emails render exactly like local ones
cover_pdf.py            turns a cover letter into the attached PDF
config.json             your credentials (git-ignored)
config.example.json     template to copy if config.json is ever lost
jobs.csv                your queue — edit this
templates/
  application.json      default email
  short.json            shorter variant
  general.json          for rows with no company name ("no_company": true)
covers/
  standard.json         cover letter, attached as a PDF
resumes/                your resumes (git-ignored): main.json points at your PDF
previews/               dry-run output
sent_log.csv            append-only record of everything sent
```
