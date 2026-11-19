# Job-search agent

A weekly job-search agent built with [Claude Code](https://claude.com/claude-code). It watches the careers pages of a curated list of companies, reads job-alert emails, screens every opening against a candidate profile, and produces a weekly digest of the jobs worth applying to, each with a link confirmed live in the same run.

It is a profile-driven job-search agent, configured for the Israeli job market. The profile, company list and rules are plain files, so it adapts to other fields and places.

## The problem

In a niche field the relevant employers are scattered: start-ups, pharma R&D sites, hospitals, universities and research institutes, each with its own careers page and hiring system (Greenhouse, Lever, Comeet, Workday, RedMatch, plain HTML, JavaScript-only pages). Job boards miss many of them, alert emails are noisy, and checking 80 sites by hand every week isn't realistic. Openings are also in two languages (Hebrew and English), and many listings are stale.

## What the agent does

- Keeps a list of target companies (`companies.csv`), maps each one to its careers page and hiring system, and adds new companies found in alert emails, with the source and reason for each.
- Every week, fetches the open jobs from every company it can read, plus the jobs in new alert emails.
- Screens them in stages, from cheap to expensive: a title filter, then opening each remaining posting to confirm it is live and in the right country, then a full profile match with a rating (strong / possible / skip), the reason, matched skills, and gaps marked as *learnable* (weeks) or *core*.
- Tracks every job across weeks (`jobs_seen.csv`): new, still open, closed, reopened, long-open. A job is never reported as new twice.
- Writes a weekly digest in Markdown and Word (right-to-left Hebrew handled), with a short run report.

## Pipeline

```
company mapping → fetch jobs → read alerts → title filter → read posting → profile match → tracker → digest
```

| Stage | What happens | Done by |
|---|---|---|
| Company mapping | Finds each new company's careers page, hiring system, board ID and local presence, verified by opening the pages | `company-mapper` subagent |
| Fetch jobs | Public hiring-system feeds where available, otherwise the rendered careers page (Playwright). Keeps jobs in the target country or with unclear location | `fetch_jobs.py` |
| Read alerts | Reads new job-alert emails (read-only), extracts company, title, location and a clean link | `read-alerts` skill + `read_alerts.py` |
| Title filter | Candidate or drop, by title only | `job-screener` subagent |
| Read posting | Opens each candidate, confirms it is live and in the right place, saves the real title and description | `read_posting.py` |
| Profile match | Rates each live job against `profile.md` | `job-screener` subagent |
| Tracker and digest | Updates `jobs_seen.csv`, writes `digests/<date>.md` and `.docx` | `make_digest.py` |

## How to run it

Requirements: Claude Code, Python 3.11+, and the packages in `requirements.txt` (`python -m pip install -r requirements.txt`, then `python -m playwright install chromium`). For alert emails, a dedicated mailbox connected through the Gmail connector.

1. Copy `profile.example.md` to `profile.md` and describe yourself: background, target roles, dealbreakers, location, rating rules and the alert mailbox. `profile.md` stays private (it is in `.gitignore`).
2. Edit `companies.csv` with your own target companies (the included list is an example for life-science and health-data companies in Israel). Unmapped companies are mapped automatically on the next run.
3. In Claude Code, run `/job-digest`. It runs every stage in order and ends with a report: jobs per source, counts after each stage, new matches, companies added and any problems.

The stages can also be run one by one (see the skills in `.claude/skills/`).

## Design choices

- **Least-privilege subagents.** Each subagent gets only the tools its job needs. The screener has no web tools at all (Read, Write, Bash), and its instructions limit Bash to the pipeline script. The company mapper can search and open web pages, and its instructions limit Bash to the page-rendering script. Neither can touch email.
- **Read-only email, enforced twice.** Google enforces it: the Gmail connection is granted only the "View your email messages and settings" permission, so it cannot send, change or delete anything. As a second layer, every connector tool that sends, drafts, forwards, deletes, labels or downloads is denied in `.claude/settings.json`, so Claude Code refuses to call it. The connected account is a separate mailbox that receives only job alerts, so personal mail is never in reach. The skill also checks that the connected account is the one named in the profile before reading anything.
- **Email and web content are data, never instructions.** Only emails from a trusted-sender list (kept in `CLAUDE.md`) are processed, matched by the sender's domain, not the display name.
- **Never applies, logs in or sends anything on the user's behalf.** Alert emails contain personal sign-in links (user IDs, login keys, one-time tokens); these are never opened or stored. Links are rewritten to the public job page, or dropped.
- **Respects site terms and bot protection.** Sites whose terms forbid scraping (e.g. LinkedIn) are never fetched; LinkedIn alert jobs are looked up on the company's own site instead. Pages behind bot protection are never bypassed; they are listed for a manual check. Requests are paced (2 seconds apart), and job boards are only used to open single postings from the user's own alerts.
- **The digest hook.** A `PreToolUse` hook (`.claude/hooks/check_digest.py`) checks every write to a digest: each job in it must have a link that read posting confirmed live in the same run. The digest script runs the same check and refuses to save otherwise, so nothing stale or invented can reach the digest.
- **Failures are visible, not silent.** A failed step is logged and the run continues where it can; problems are listed at the top of the digest and in the report. If screening fails, the tracker is left untouched rather than marking every open job as closed.
- **The run-date fix.** A full run can pass midnight, and scripts that default to "today" would then look for the next day's files. The run date is fixed when the run begins and passed to every later step.
- **Safe, repeatable state.** Email state is saved only after a whole run succeeds; re-running a day gives the same result; earlier outputs are archived, never deleted.

## Limitations

- Careers pages change. A page redesign can break job extraction for a company until it is re-mapped; companies behind bot protection or without a careers page rely on alert emails and manual checks.
- Ratings are an LLM's judgment against a written profile. They are a shortlist, not a decision, and the profile has to be written carefully.
- Titles in some alert emails are truncated; the real title is only known after the posting is opened.
- LinkedIn jobs that can't be found on the company's own site are listed without a link.
- Location rules are tuned for Israel (Hebrew and English place names).
- A full run opens many pages and takes a while (tens of minutes for ~80 companies).

## Planned extensions

- Run `/job-digest` automatically every week on a schedule and send the digest to the user.
- Re-map companies whose careers pages stop yielding jobs.
- More hiring-system feeds, and a per-company history of how many jobs each source produced.

## Project layout

```
CLAUDE.md                     rules for the agent (sources, safety, screening, reporting)
profile.example.md            profile format (copy to profile.md)
companies.csv                 target companies and their careers pages
rejected_companies.csv        companies removed, with reasons (never re-added)
.claude/agents/               company-mapper and job-screener subagents
.claude/skills/               job-digest, fetch-jobs, read-alerts, read-posting, weekly-digest
.claude/hooks/check_digest.py digest link check (PreToolUse hook)
scripts/render_page.py        renders JavaScript careers pages (Playwright)
```

## License

MIT; see `LICENSE`.
