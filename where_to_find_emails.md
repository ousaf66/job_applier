# Where the email addresses actually come from

I checked a batch of Pakistani and remote AI employers to fill `jobs.csv`. The honest
finding, before you build a big list:

**Almost nobody accepts applications by email any more.** Arbisoft, Red Buffer, Emumba,
Confiz, Systems Limited, NETSOL and Folio3 all route hiring through an ATS portal.
Several don't publish any email at all; Tkxel obfuscates theirs against scrapers.
LinkedIn job posts have an Apply button, never an address — and scraping LinkedIn for
addresses breaks their terms and risks your account.

So this tool is a **supplement**, not a replacement for applying on the portals.

## Where email applications genuinely still work

1. **Recruiter posts on LinkedIn** — the "share your CV at hiring@company.com" posts.
   This is the single best source. Search LinkedIn for `"share your CV" AI engineer`
   or `"send your resume to" machine learning`, filter to Past Week, and copy the
   address into `jobs.csv`. I can help you sweep these in a browser session.
2. **Smaller startups and AI labs** — under ~50 people, a real person reads `hello@`
   or `careers@`. Densight Labs in `jobs.csv` is exactly this case: their careers page
   asks for a CV and a three-sentence note.
3. **Facebook / WhatsApp job groups** for Pakistani devs — messy, but address-rich.
4. **Direct to a hiring manager** — find the AI/Engineering lead on the company site or
   LinkedIn, guess `firstname@company.com`, verify it with a free checker
   (hunter.io, neverbounce) before adding it. One well-aimed email beats twenty to `info@`.
5. **Referrals** — anyone from FAST-NUCES already at the company. Highest conversion by far.

## Verified so far

| Company | Address | Reality check |
|---|---|---|
| Densight Labs | hello@densightlabs.com | Genuinely invites CVs. Send. |
| Arbisoft | contact@arbisoft.com | General inbox, ATS preferred. Low yield. |
| Red Buffer | info@redbuffer.ai | General inbox, ATS preferred. Low yield. |
| EdgeFirm | hello@edgefirm.io | **Best find of this pass.** ~16 engineers, Karachi hybrid. Careers page asks for a CV plus a short note on what you want to build. Live opening: Senior AI Engineer. Send. |
| InvoZone | careers@invozone.com | A real careers inbox, not an info@ — "You can also send your resume to careers@invozone.com". But their live openings would not render, so confirm the exact job title before sending. Held in jobs.csv as `status=hold`. |

## Apply on the portal instead (no email exists)

- Arbisoft — https://arbisoft.hirestream.io/careers/jobs/
- Red Buffer — https://redbuffer.ai/careers/
- Emumba — https://emumba.pinpointhq.com/
- Confiz — https://confiz.simplicant.com/
- Systems Limited — https://www.systemsltd.com/careers
- NETSOL — https://careers.netsoltech.com/
- Densight Labs — https://densightlabs.com/apply
- Kwanso — https://jobs.lever.co/kwanso/ (careers page redirects straight to Lever)

## Checked 2026-08-30 — no usable address

Recorded so this pass doesn't get repeated. All of these were opened and read, not guessed:

| Company | What's actually there | Verdict |
|---|---|---|
| Xeven Solutions | `info@xevensolutions.com`, and "No vacancies open right now" | Skip — general inbox, nothing open |
| Ebryx | `securenow@ebryx.com` (a sales/contact inbox), Apply buttons into an ATS | Skip — no AI/ML roles listed |
| Kwanso | careers page 307-redirects to Lever | Portal only |
| Tintash, Antematter, Datics AI, DPL, Techverx, Cogent Labs, Enterprise64 | careers URLs returned 404/403 | Unverified — URLs may have moved, worth a manual look |

Search engines were also a dead end for this: job-board aggregators (Indeed, Glassdoor,
Rozee, Himalayas) surface the posting but never the address, which is the same wall the
first pass hit. **The hit rate for "company careers page publishes a real application
email" is roughly 1 in 5, and only the small ones pay off.** Budget your time accordingly —
sweeping recruiter posts (source 1 below) is a far better use of an hour than crawling
company sites.

## Rule of thumb

A personalised email to a named person at a company that publishes an address is a
job application. Fifty identical emails to scraped `info@` addresses is spam — it
gets your Gmail flagged and your name remembered for the wrong reason. Keep the list
small and targeted and this tool earns its keep.
