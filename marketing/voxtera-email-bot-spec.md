# Voxtera Email Outreach Bot — Spec

**Owner:** Dan Catalin Dinu
**Purpose:** Personalized cold outreach to hotel decision-makers for Voxtera design-partner recruitment
**Scope:** Minimal viable bot for the first 10 hotels, designed to scale up later
**Stack:** Python 3.11+, Anthropic SDK, Zoho SMTP
**Status:** Spec — not yet built

---

## 1. Goal

Send personalized, founder-style cold emails from `dan@voxtera.io` to 10 carefully chosen hotels, follow up with a 4-touch sequence over 12 days, and land 1-3 discovery calls that produce the first Voxtera design partner.

This is the **minimal version** of a bigger outreach bot. It is deliberately under-engineered for 10 prospects. When (if) the campaign expands beyond ~50 prospects, the architecture upgrades to Google Sheets state, reply automation, and a scheduler.

---

## 2. What the bot does (and does not)

### Does

- Reads a local CSV of prospects (`prospects.csv`)
- For each prospect, generates a personalized Touch 1 email via Claude (Sonnet 4.6)
- Writes drafts back to the CSV for human review
- Sends approved drafts via Zoho SMTP, one at a time, with a configurable delay
- Tracks which touch each prospect has received and when
- Sends Touches 2, 3, 4 on schedule (Day 4, 8, 12 after Touch 1) — partially templated, lightly personalized
- Logs every send with timestamp + Zoho message ID

### Does NOT

- Read or classify replies (you handle replies manually in Zoho webmail)
- Auto-pause sequences when prospects reply (you mark `status=paused-replied` in the CSV by hand)
- Schedule sends throughout the day (you run it manually when ready)
- Integrate with Google Sheets, Slack, or any external tool
- Scrape or auto-research prospects (you fill the CSV by hand for now)
- Handle bounces, complaints, or unsubscribes automatically

---

## 3. Data model — `prospects.csv`

One row per prospect. CSV with headers, editable in Excel / Numbers / any text editor.

| Column | Type | Purpose |
|---|---|---|
| `id` | int | Unique row ID (1, 2, 3…) |
| `first_name` | string | GM's first name — used in greeting |
| `last_name` | string | GM's last name |
| `email` | string | Target email address |
| `role` | string | "General Manager" / "Guest Experience Director" / etc. |
| `property_name` | string | Hotel name, e.g. "Hôtel des Grands Boulevards" |
| `city` | string | "Paris" / "Barcelona" / etc. |
| `country` | string | "France" / "Spain" / etc. |
| `star_rating` | int | 4 or 5 |
| `rooms` | int | Approximate room count |
| `website` | string | Hotel website URL |
| `signal` | string | A real, specific detail to reference in Touch 1 (e.g. "Recent renovation of suites, March 2026") |
| `language_mix` | string | Notes on guest international mix (e.g. "70% international, top: US, JP, BR") |
| `status` | enum | `pending` / `t1-drafted` / `t1-sent` / `t2-sent` / `t3-sent` / `t4-sent` / `paused-replied` / `closed` |
| `t1_draft` | string | Generated Touch 1 body (filled by bot) |
| `t1_subject` | string | Generated Touch 1 subject (filled by bot) |
| `t1_approved` | bool | `true` / `false` — gates the send |
| `t1_sent_at` | datetime ISO | When Touch 1 was sent |
| `t2_sent_at` | datetime ISO | When Touch 2 was sent |
| `t3_sent_at` | datetime ISO | When Touch 3 was sent |
| `t4_sent_at` | datetime ISO | When Touch 4 was sent |
| `notes` | string | Founder freeform notes |

### Workflow on the CSV

1. **You manually fill** the first 13 columns (id through language_mix) for each hotel before running the bot
2. **Bot fills** `t1_subject` and `t1_draft` when you run `generate-touch1`
3. **You review** each row in Excel/Numbers, edit drafts if needed, set `t1_approved=true` on rows you want to send
4. **Bot sends** approved rows when you run `send-touch1` and updates `status` + `t1_sent_at`
5. **Bot sends Touches 2-4** when their scheduled day arrives (run `send-followups` daily)
6. **You manually update** `status=paused-replied` if a prospect replies (Zoho webmail is your reply UI)

---

## 4. The 4-touch sequence

| Touch | Day | Purpose | Content origin |
|---|---|---|---|
| **Touch 1** | Day 1 | Personalized opener. Names the property, references the `signal`, leads with the language-barrier pain. No link. No demo. Ends with a soft ask: "open to a 15-min conversation?" | LLM-generated per prospect |
| **Touch 2** | Day 4 | Short follow-up. "Just bumping this up — wanted to share the demo." Includes a link to a 60-second demo video. | Templated, light personalization |
| **Touch 3** | Day 8 | Introduces the Founding Hotels offer + scarcity ("10 spots, free 60-day pilot"). | Templated |
| **Touch 4** | Day 12 | Polite close. "Should I close your file?" Surprisingly effective. | Templated |

Touch 1 is where 80% of personalization effort goes. Touches 2-4 are mostly stock — they just need to land in the same thread and reference the property name.

### Sample Touch 1 (illustrative, LLM will generate fresh per prospect)

```
Subject: a question about your international guests

Hi Marie,

Saw the recent suite renovation at Hôtel des Grands Boulevards — beautiful
work, especially the new junior suites.

Quick question on the guest side. With a 70% international mix in central
Paris, how do you currently handle late-night requests in languages your
front desk doesn't cover? Asking because I'm building something for exactly
that moment, and I'd love your read on whether it's a real problem or
something I'm overestimating.

15-min call this week or next?

Dan
```

Notice: short, lowercase opening, names a specific real thing, lands one question, asks for a small ask. No link. No "I'd love to show you a demo." No "looking forward to hearing from you."

---

## 5. Repo structure

```
voxtera-outreach/
├── pyproject.toml
├── README.md
├── .env                          # secrets (gitignored)
├── .env.example                  # template
├── .gitignore
├── prospects.csv                 # the source of truth (gitignored)
├── prospects.example.csv         # template with column headers + 1 fake row
├── src/voxtera_outreach/
│   ├── __init__.py
│   ├── config.py                 # constants, env loading
│   ├── csv_io.py                 # read/write the CSV
│   ├── personalise.py            # Claude prompt + call for Touch 1 generation
│   ├── templates/
│   │   ├── touch2.txt            # static template, with {placeholders}
│   │   ├── touch3.txt
│   │   └── touch4.txt
│   ├── sender.py                 # Zoho SMTP send + threading headers
│   ├── scheduler.py              # decides who's due for which touch today
│   └── cli.py                    # `generate-touch1`, `send-touch1`, `send-followups`
└── tests/
    └── test_csv_io.py
```

### Why CSV not Google Sheets

For 10 prospects: simpler, no API setup, you can edit in Numbers/Excel, no auth ceremony. If we scale beyond 50, we revisit.

### Why no scheduler daemon

For 10 prospects: you run the bot manually 2-3 times per week. A cron job and IMAP poller add complexity that's not justified at this volume.

---

## 6. Configuration (`.env`)

```bash
# Anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Zoho SMTP
ZOHO_SMTP_HOST=smtp.zohocloud.ca
ZOHO_SMTP_PORT=465
ZOHO_USERNAME=dan@voxtera.io
ZOHO_APP_PASSWORD=fBxbLrwVtCQ1

# Sender identity
SENDER_NAME=Dan Dinu
SENDER_LINKEDIN_URL=https://www.linkedin.com/in/...   # TODO: fill in

# Sending behavior
DAILY_CAP=10                   # hard cap, won't send more than this in one run
SEND_DELAY_SECONDS=60          # pause between sends (avoids burst pattern)
DRY_RUN=false                  # if true, print emails instead of sending

# Product context (used in LLM prompts)
PRODUCT_NAME=Voxtera
PRODUCT_PITCH=Real-time multilingual voice concierge for hotels.
DEMO_VIDEO_URL=                # filled in once demo is ready
BOOKING_LINK=                  # Cal.com / Calendly link for Touch 3
```

`.env` is gitignored. Never commits to repo. `.env.example` ships in the repo as a template with empty values.

---

## 7. CLI commands

Built with `argparse` for simplicity. No fancy framework.

### `generate-touch1`

Reads all rows with `status=pending`, calls Claude for each, writes Subject + Body to CSV columns, sets `status=t1-drafted`.

```bash
$ python -m voxtera_outreach generate-touch1
Reading prospects.csv (12 rows)
Found 8 prospects with status=pending
Generating Touch 1 drafts:
  [1/8] Marie Dubois @ Hôtel des Grands Boulevards … done (1.2s)
  [2/8] Carlos Mendes @ Hotel Casa Fuster … done (1.1s)
  [3/8] Sofia Bianchi @ Hotel de la Ville … done (1.3s)
  ...
Wrote 8 drafts back to prospects.csv
Next step: open prospects.csv, review t1_draft + t1_subject, set t1_approved=true on rows to send
```

### `send-touch1`

Reads all rows with `status=t1-drafted` AND `t1_approved=true`. For each: sends via Zoho SMTP, sets `status=t1-sent`, records `t1_sent_at`, sleeps `SEND_DELAY_SECONDS`. Respects `DAILY_CAP`.

```bash
$ python -m voxtera_outreach send-touch1
Reading prospects.csv (12 rows)
Found 5 approved drafts ready to send
Daily cap: 10, send delay: 60s
Sending:
  [1/5] → marie.dubois@grandsboulevards.fr … sent (msgid: <abc@zoho>) sleeping 60s
  [2/5] → carlos.mendes@hotelcasafuster.com … sent sleeping 60s
  ...
Sent 5 emails. Updated prospects.csv.
```

### `send-followups`

Checks every row whose `status` is `t1-sent`, `t2-sent`, or `t3-sent`. If the appropriate number of days has elapsed since the last touch (4 days for T2, 4 more for T3, 4 more for T4), generates the follow-up from the template, sends via Zoho, updates status. Respects daily cap.

Critical: follow-ups go **in the same email thread** as Touch 1 (using `In-Reply-To` and `References` headers). This makes them look like real conversation, not standalone cold mail.

```bash
$ python -m voxtera_outreach send-followups
Reading prospects.csv (12 rows)
Checking due follow-ups:
  Marie Dubois: T1 sent 4 days ago → T2 due → sending
  Carlos Mendes: T1 sent 2 days ago → T2 not due yet
  Sofia Bianchi: T2 sent 4 days ago → T3 due → sending
  Pierre Laurent: T3 sent 5 days ago → T4 due → sending
Sent 3 follow-ups.
```

### `status` (optional, nice-to-have)

Prints a summary of the prospect pipeline.

```bash
$ python -m voxtera_outreach status
Voxtera Outreach Pipeline
─────────────────────────
Total prospects: 10
  pending:           2
  t1-drafted:        0
  t1-sent:           3
  t2-sent:           2
  t3-sent:           1
  t4-sent:           1
  paused-replied:    1
  closed:            0

Sent today: 0
Sent this week: 6
```

---

## 8. The Touch 1 LLM prompt

This is the most important piece of the whole bot. The system prompt:

```
You are writing a single cold outreach email from Dan Dinu, founder of
Voxtera, to a hotel general manager. Your job is to draft an email that
reads as if Dan wrote it himself in 90 seconds — direct, specific,
human, slightly imperfect.

CONTEXT — Voxtera:
{PRODUCT_PITCH}

CONTEXT — the recipient:
- Name: {first_name} {last_name}
- Role: {role}
- Property: {property_name} ({star_rating}★, {rooms} rooms, {city})
- Real signal to reference: {signal}
- Guest mix: {language_mix}

WRITE A SHORT EMAIL with:
- Subject line: lowercase, 4-7 words, no clickbait, sounds like a normal
  founder-to-founder note. Reference the property or the guest-experience
  problem.
- Body: 4-6 sentences, lowercase opening ("hi {first_name},"), 100-130
  words max. Include:
  1. One specific, real reference to the {signal}
  2. One concrete observation about the language-barrier problem at hotels
     of their type
  3. One question that invites a reply, not a yes/no
  4. A soft ask: "15-min call this week or next?" or similar
- Sign-off: "Dan" — nothing else. No "looking forward to hearing from you."

HARD RULES:
- No "I hope this email finds you well"
- No "I'm reaching out because"
- No three-paragraph mini-pitch
- No links, no demo offer, no "I'd love to show you"
- No corporate language, no exclamation points, no em-dashes used as bullets
- No promised ROI numbers, no fake stats
- Use the signal naturally — if it doesn't fit, mention something else
  real from the property
- It should be obvious from sentence 1 that this is not mass-mailed

OUTPUT FORMAT (strict JSON):
{
  "subject": "...",
  "body": "..."
}
```

The `body` field is the email body. The bot appends the signature automatically (name + LinkedIn URL).

### Why these constraints

The single biggest tell that an email is mass-LLM-generated is **over-polished prose**. Real founder mail is lowercase, slightly clipped, occasionally awkward. The prompt explicitly bans the corporate phrases that make LLM cold mail recognizable.

---

## 9. The follow-up templates

Stored as text files in `templates/`. Curly-brace placeholders are filled at send time.

### `templates/touch2.txt`

```
Subject: re: {original_subject}

hi {first_name},

quick bump on this. wanted to share a 60-second demo we put together:

{DEMO_VIDEO_URL}

it shows how guests speak in any language and get answered in the same one.
might be the easiest way to see if it's something for {property_name}.

Dan
```

### `templates/touch3.txt`

```
Subject: re: {original_subject}

hi {first_name},

since the demo, a couple of things have shifted on our side that might
matter for you.

we're taking on 10 hotels as founding partners — 60-day pilot, white-glove
setup, no cost. {property_name} fits what we're looking for almost too well.

if interested: {BOOKING_LINK}

Dan
```

### `templates/touch4.txt`

```
Subject: re: {original_subject}

hi {first_name},

last one from me — should I close your file? totally fine either way, just
don't want to keep landing in your inbox if it's not the right time.

Dan
```

All follow-ups send as **replies in the same thread** (`In-Reply-To` header set to the original Message-ID), so the recipient sees one continuous conversation, not four disconnected emails.

---

## 10. Email signature

Appended to every email automatically:

```
Dan
Voxtera · https://www.linkedin.com/in/...

```

No phone, no full title, no logo, no "Schedule a call" widget. Founder-style is sparse.

---

## 11. Deliverability safeguards

Built into `sender.py`:

- **Daily cap** of 10 sends per `send-touch1` run (configurable). Won't exceed even if more rows are approved.
- **Send delay** of 60 seconds between consecutive emails. Real humans don't fire 10 emails in 3 seconds.
- **No tracking pixels** ever. Pixels are a deliverability tell on plain-text founder mail.
- **No wrapped/redirected links**. Use raw URLs for the demo video and booking link.
- **Plain-text only** in v1. No HTML, no images. Plain-text is the most deliverable format for cold outreach.
- **In-Reply-To threading** for follow-ups — makes them look conversational, not cold.
- **Domain warmup** runs in parallel via Warmup Inbox to build sender reputation.

---

## 12. Out of scope for v1

Documented here so they don't get sneaked in:

- ❌ Google Sheets integration
- ❌ Reply detection / classification / auto-pause
- ❌ Slack notifications
- ❌ Bounce handling automation
- ❌ Unsubscribe link / GDPR compliance flow
- ❌ Multi-language email generation
- ❌ A/B testing of subject lines
- ❌ Analytics dashboard
- ❌ Web UI of any kind
- ❌ Multi-sender / inbox rotation

If the first 10 emails produce 2-3 conversations and Voxtera gets its design partner, **none of this matters**. If we want to ramp to 600 prospects after that win, we revisit.

---

## 13. Build sequence

Estimated total: ~4-6 hours of focused work.

| Step | What | Time |
|---|---|---|
| 1 | Repo setup, pyproject.toml, .env, CSV loader | 30 min |
| 2 | Zoho SMTP sender — send one hardcoded email end to end | 45 min |
| 3 | Touch 1 LLM prompt + generator (no UI yet) | 45 min |
| 4 | CLI: `generate-touch1` writes drafts to CSV | 30 min |
| 5 | CLI: `send-touch1` reads approved rows and sends | 30 min |
| 6 | Templates + `send-followups` with thread-reply headers | 60 min |
| 7 | Smoke test end-to-end with 2 fake prospects (your own gmail accounts) | 30 min |
| 8 | Populate real 10 prospects, generate drafts, review | 60 min |
| 9 | Send Touch 1 to real prospects | 5 min |

---

## 14. Success criteria

The bot succeeds if:

1. ✅ 10 prospects get a personalized Touch 1 that doesn't read as AI-generated
2. ✅ Touches 2-4 send automatically on schedule in the same thread
3. ✅ ≥1 hotel replies positively and books a discovery call
4. ✅ 0 of the sends end up reported as spam
5. ✅ Founder spent < 30 minutes/day operating the bot during the campaign

The bot **fails** if:

1. ❌ Replies look like every other cold email the GM gets
2. ❌ Threading breaks and follow-ups arrive as standalone messages
3. ❌ Daily cap is bypassed and Zoho throttles the inbox
4. ❌ Anyone marks a message as spam (single complaint can hurt domain reputation)

---

## 15. What happens after the first hotel

Once one hotel signs as a design partner, this bot's job is done. From that point:

- **Either** we keep using it as-is for the next 20-50 prospects to deepen the pilot pool
- **Or** we rebuild against the full campaign brief (Sheets, IMAP, Slack, scheduler, 600 prospects)

The minimal-bot architecture is intentionally a **disposable scaffold**. It is not designed to grow gracefully to 600 prospects. When the time comes, we throw most of it out and rebuild.

---

*Document version: 1.0*
*Last updated: 22 May 2026*
