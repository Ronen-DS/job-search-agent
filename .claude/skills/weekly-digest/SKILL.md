---
name: weekly-digest
description: Builds the weekly job digest (digests/<date>.md) from jobs/<date>_screened.csv and updates the job tracker jobs_seen.csv (new / still open / closed / reopened / long-open). Use as the last step of the weekly job search, after fetch-jobs and read-posting.
---

# Weekly digest

Run from the project root, after fetch-jobs and read-posting for the same date:

```
python .claude/skills/weekly-digest/make_digest.py            # today
python .claude/skills/weekly-digest/make_digest.py 2026-11-18 # a given date
```

## Tracker: jobs_seen.csv
Columns: url, company, title, first_seen, last_seen, status (open / closed), rating, my_decision. Jobs are identified by URL. Every rated job (strong, possible or skip) is tracked, but only strong and possible jobs appear in the digest.
- **New:** a URL seen for the first time. If the same company had a job with the same title under another URL, the note "similar to a job seen on <date>" is added.
- **Still open from earlier weeks:** seen again and still live, shown with first_seen. After more than 8 weeks it moves to **Long-open**.
- **Closed since last week:** open last time but not live in this run. The job is marked closed and listed once.
- **Reopened:** a closed URL that is live again. It is reported as new, with a note.
- **my_decision:** type `applied` or `not interested` in the file. Such jobs are never reported as new again, and "not interested" jobs are left out of the other job sections too.

Read posting re-checks every job still open in the tracker, so "Still open" jobs are confirmed live in each run.

Re-running for the same date gives the same result. The tracker as it was before that date is kept in `jobs/<date>_jobs_seen_before.csv`, and `my_decision` values typed in since are kept.

## Digest sections
New strong matches, New possible matches, Still open from earlier weeks, Closed since last week, Check manually, LinkedIn only: check manually, No careers page: consider direct outreach, Long-open.
- LinkedIn only: LinkedIn alert jobs (read-alerts skill) that read posting couldn't find on the company's own site (status `linkedin_only` in `jobs/<date>_read_posting.json`). Shown with company, title and reason, with no link, because linkedin.com is never opened.
- In each job section, in-field jobs come first. Out-of-field jobs (`field` = out in screened.csv) follow after a bold "Out-of-field roles" line.
- High-priority companies (column `priority` = high in companies.csv, marked ★) come first within the in-field and the out-of-field groups.
- Each job shows: company, title, location, link, why it fits, gaps, and rating and first_seen for jobs from earlier weeks.
- Check manually: companies with ats `blocked`, everything in check_manually.csv, and companies whose fetch failed in this run (from `jobs/<date>_fetch_log.json`).
- No careers page: companies with ats `none`.

## Word copy: digests/<date>.docx
After the .md passes the link check and is saved, make_digest.py also saves `digests/<date>.docx` (needs python-docx, see requirements.txt), made from the saved .md by `digest_docx.py`:
- Section headings are Heading 1 and jobs are Heading 2 (they show in Word's navigation pane). Every link is clickable.
- Lines that start with Hebrew are right-to-left (right-aligned, bullet on the right). Hebrew inside English lines is marked right-to-left, so mixed lines display in the right order.
- If the .docx can't be made, the .md and the tracker stay saved, "DOCX NOT saved" is printed and the exit code is 1.

To remake the Word copy of an existing digest (it is refused if the .md fails the link check):

```
python .claude/skills/weekly-digest/digest_docx.py digests/2026-11-18.md
```

## Only links confirmed by read posting
Every job in the job sections must have a link that read posting confirmed live in this run (`jobs/<date>_read_posting.json`). `make_digest.py` refuses to save a digest that fails this check. The same check, `.claude/hooks/check_digest.py`, also runs as a PreToolUse hook on Write / Edit / MultiEdit of `digests/*.md`. To check a digest by hand:

```
python .claude/hooks/check_digest.py digests/2026-11-18.md
```
