# Deploying to Vercel

The whole app runs on Vercel: a static page in `public/`, Python serverless functions in
`api/`, and an Upstash Redis database added through the Vercel dashboard. Nothing else.

---

## 1. Get the code to Vercel

The project is not a git repository yet:

```bash
cd ~/personal_projects/job_apply_bot
git init
git add -A
git commit -m "Job application mailer"
```

Create an **empty private** repo on github.com, then:

```bash
git remote add origin https://github.com/<you>/job_apply_bot.git
git push -u origin main
```

At vercel.com: **Add New → Project → Import** that repo. Take every default and deploy.
The first deploy will show an error page — that is expected until step 2 and 3 are done.

`config.json` and your local `jobs.csv` are excluded by both `.gitignore` and
`.vercelignore`, so your Gmail app password never leaves this Mac.

## 2. Add the database

In the project: **Storage → Create Database → Upstash for Redis → Connect**.

That injects `KV_REST_API_URL` and `KV_REST_API_TOKEN` automatically. Nothing to copy.

## 3. Add four environment variables

**Settings → Environment Variables**, all environments:

| Name | Value |
|---|---|
| `GMAIL_ADDRESS` | `yousaf.hasan66@gmail.com` |
| `GMAIL_APP_PASSWORD` | the 16-character Google App Password |
| `UI_PASSWORD` | a long password you invent — this is your login |
| `SESSION_SECRET` | any long random string |

Then **Deployments → ⋯ → Redeploy**. Environment variables only apply to builds that
come after them.

## 4. First run

Open your `*.vercel.app` URL and log in with `UI_PASSWORD`.

1. **Setup tab** — every row should read OK. Upload your resume PDF (it goes into the
   database, not the repo). Fill in your name, phone, LinkedIn, GitHub.
2. **Queue tab** — your five companies are already there, seeded from `jobs.csv`.
3. **Dry run** → read a couple of previews.
4. **Send test to myself** → check the attachment opens.
5. **Send for real.**

---

## Things that are different from the local version

**Keep the tab open while sending.** A serverless function cannot sleep for 45 seconds
between emails — it would be killed. So the browser sends one email per request and does
the waiting itself. Close the tab and the batch stops where it is; already-sent rows stay
marked sent, so restarting picks up where it left off.

**Your data lives in Redis, not in files.** The queue, the sent log, the templates and the
resume are all in the database. `jobs.csv` in the repo is only a seed for the very first
request.

**Secrets live in Vercel, not in the app.** The Setup tab shows whether `GMAIL_APP_PASSWORD`
is set but can never display or change it. To change it, edit the environment variable and
redeploy.

**A failed send parks the row as `failed`** rather than retrying forever. Set it back to
blank in the Queue tab to try again.

## If sending fails with a connection error

Gmail SMTP over port 465 usually works from Vercel, but it is the one part of this that
cannot be verified without deploying. If every send times out, the platform is blocking
outbound SMTP. The code already has a fallback that sends over HTTPS instead — set two
more environment variables and redeploy:

| Name | Value |
|---|---|
| `RESEND_API_KEY` | an API key from resend.com |
| `RESEND_FROM` | `you@yourdomain.com` |

The catch: Resend will not let you send *as* a gmail.com address. You need a domain you
own and have verified with them. Applications would arrive from that domain, with your
Gmail set as the reply-to.

## Limits worth knowing

- Hobby plan functions cap at 60 seconds. One email takes a few seconds, so this is fine.
- Gmail allows roughly 500 messages a day; 25–30, spaced out, is the safe pace.
- Upstash's free tier is far more than this app will ever use.
