# Builds the weekly digest (digests/<date>.md) and updates the job tracker
# (jobs_seen.csv).
#
# Inputs (the date defaults to today):
#   jobs/<date>_screened.csv    rated jobs, all confirmed live by read posting in this run
#   jobs/<date>_fetch_log.json  fetch summary, for errors (if present)
#   companies.csv, check_manually.csv, jobs_seen.csv
#
# Tracker rules (jobs are identified by URL):
#   - First time a URL is seen: reported as new (strong / possible only).
#     If the same company had a job with the same title under another URL,
#     the note "similar to a job seen on <date>" is added.
#   - Seen again while open: listed under "Still open from earlier weeks",
#     or under "Long-open" once it has been open for more than 8 weeks.
#   - Open last time but not live now: marked closed and listed once under
#     "Closed since last week".
#   - Closed and live again with the same URL: "reopened", reported as new.
#   - my_decision "applied" or "not interested": never reported as new again
#     ("not interested" jobs are also left out of the other job sections).
#
# Re-running for the same date gives the same result: before the first
# update of a date, jobs_seen.csv is copied to jobs/<date>_jobs_seen_before.csv,
# and later runs for that date start from that copy (keeping any
# my_decision values you have typed in since).
#
# Usage (from the project root):
#   python .claude/skills/weekly-digest/make_digest.py [YYYY-MM-DD]

import csv
import json
import re
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for digest_docx
from check_digest import validate  # noqa: E402  (same check as the hook)

JOBS_DIR = PROJECT_ROOT / "jobs"
DIGESTS_DIR = PROJECT_ROOT / "digests"
TRACKER = PROJECT_ROOT / "jobs_seen.csv"
COMPANIES = PROJECT_ROOT / "companies.csv"
CHECK_MANUALLY = PROJECT_ROOT / "check_manually.csv"

TRACKER_COLUMNS = ["url", "company", "title", "first_seen", "last_seen",
                   "status", "rating", "my_decision"]
REPORTED_RATINGS = {"strong", "possible"}
NEVER_NEW = {"applied", "not interested"}
LONG_OPEN_DAYS = 8 * 7


# ---------------------------------------------------------------------------
# Reading and writing files
# ---------------------------------------------------------------------------
def read_csv(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:  # -sig: handles Excel's BOM
        return list(csv.DictReader(f))


def write_tracker(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKER_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in TRACKER_COLUMNS})


def load_tracker(day):
    """The tracker as it was before this date's first run (see top)."""
    before_path = JOBS_DIR / f"{day}_jobs_seen_before.csv"
    if before_path.exists():
        tracker = read_csv(before_path)
        # Keep any decisions typed into jobs_seen.csv since then
        decisions = {r["url"]: r.get("my_decision", "") for r in read_csv(TRACKER)}
        for row in tracker:
            row["my_decision"] = decisions.get(row["url"], row.get("my_decision", ""))
    else:
        tracker = read_csv(TRACKER)
        if TRACKER.exists():
            shutil.copy(TRACKER, before_path)
        else:
            write_tracker(before_path, [])  # an empty "before" for the first run
    return tracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def same_title_key(company, title):
    """Company + title with position numbers and punctuation removed, to
    spot the same job reposted under a new URL."""
    title = re.sub(r"\(\d+\)", "", title.lower())
    title = re.sub(r"[^\w]+", " ", title).strip()
    return company.strip().lower(), title


def make_ident(shared_urls):
    """Jobs are identified by URL. Some pages list several jobs with no
    page of their own (e.g. Protai's careers page), so for a URL used by
    more than one job, the title is added to tell those jobs apart."""
    def ident(url, title):
        if url in shared_urls:
            return f"{url} :: {same_title_key('', title)[1]}"
        return url
    return ident


def days_between(start, end):
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def decision(row):
    return row.get("my_decision", "").strip().lower()


# ---------------------------------------------------------------------------
# Main work
# ---------------------------------------------------------------------------
def update_tracker(day, tracker, live_jobs, ident):
    """Update tracker rows in place. live_jobs is keyed by ident(url, title).
    Returns the jobs for each digest section."""
    before = [dict(r) for r in tracker]  # tracker before this run, for "similar"
    by_ident = {ident(r["url"], r["title"]): r for r in tracker}
    sections = {"new": [], "still_open": [], "long_open": [], "closed": []}

    for key, job in live_jobs.items():
        row = by_ident.get(key)
        rating = job["rating"]

        if row is None:
            # Never seen before: new
            row = {"url": job["url"], "company": job["company"], "title": job["title"],
                   "first_seen": day, "last_seen": day, "status": "open",
                   "rating": rating, "my_decision": ""}
            tracker.append(row)
            by_ident[key] = row
            key = same_title_key(job["company"], job["title"])
            title_key = same_title_key(job["company"], job["title"])
            similar = [r for r in before
                       if same_title_key(r["company"], r["title"]) == title_key]
            note = ""
            if similar:
                note = f"similar to a job seen on {min(r['first_seen'] for r in similar)}"
            if rating in REPORTED_RATINGS:
                sections["new"].append((job, row, note))
            continue

        was_closed = row["status"] == "closed"
        row.update({"last_seen": day, "status": "open", "rating": rating})
        if rating not in REPORTED_RATINGS or decision(row) == "not interested":
            continue
        if was_closed:
            # Same URL back after being closed: reopened, reported as new
            if decision(row) not in NEVER_NEW:
                sections["new"].append((job, row, f"reopened (first seen {row['first_seen']})"))
        elif days_between(row["first_seen"], day) > LONG_OPEN_DAYS:
            sections["long_open"].append((job, row, ""))
        else:
            sections["still_open"].append((job, row, ""))

    # Open before, not live in this run: closed
    for row in tracker:
        if row["status"] == "open" and ident(row["url"], row["title"]) not in live_jobs:
            row["status"] = "closed"
            if row.get("rating") in REPORTED_RATINGS and decision(row) != "not interested":
                sections["closed"].append((None, row, ""))
    return sections


def company_lists(day, companies):
    """Rows for 'Check manually' and 'No careers page'."""
    check = {}
    for c in companies:
        if c["ats"].strip() == "blocked":
            check[c["name"]] = (c["careers_url"] or c["website"],
                                "careers page behind bot protection")
    for c in read_csv(CHECK_MANUALLY):
        check[c["name"]] = (c["careers_url"], c["reason"])
    log_path = JOBS_DIR / f"{day}_fetch_log.json"
    if log_path.exists():
        by_name = {c["name"]: c for c in companies}
        for entry in json.loads(log_path.read_text(encoding="utf-8")):
            if entry["method"] == "error" and entry["company"] not in check:
                company = by_name.get(entry["company"], {})
                check[entry["company"]] = (company.get("careers_url", ""),
                                           f"fetch error this run: {entry['note']}")
    no_page = [c for c in companies if c["ats"].strip() == "none"]
    return check, no_page


def linkedin_only_jobs(day):
    """LinkedIn alert jobs that read posting could not find on the
    company's own site (status 'linkedin_only' in read_posting.json)."""
    path = JOBS_DIR / f"{day}_read_posting.json"
    if not path.exists():
        return []
    return [r for r in json.loads(path.read_text(encoding="utf-8"))
            if r.get("status") == "linkedin_only"]


def run_problems(day):
    """Steps of the /job-digest run that failed or were incomplete
    (jobs/<date>_run_log.json, written by .claude/skills/job-digest/pipeline.py)."""
    path = JOBS_DIR / f"{day}_run_log.json"
    if not path.exists():
        return []
    return [f"{e['step']}: {e['status']}: {e['detail']}"
            for e in json.loads(path.read_text(encoding="utf-8")) if e["status"] != "ok"]


def new_companies(day, companies):
    """Companies added to companies.csv in the 7 days up to this digest."""
    return [c for c in companies
            if c.get("date_added") and 0 <= days_between(c["date_added"], day) < 7]


def build_digest(day, sections, check, no_page, priority, counts, linkedin=(),
                 problems=(), added=()):
    def order(company):
        return (0 if priority.get(company) == "high" else 1, company.lower())

    def star(company):
        return " ★" if priority.get(company) == "high" else ""

    def job_block(job, row, note, show_first_seen):
        lines = [f"### {job['company']}{star(job['company'])}: {job['title']}",
                 f"- Location: {job['location'] or 'unclear'}",
                 f"- Link: {job['url']}",
                 f"- Why it fits: {job['reason']}",
                 f"- Gaps: {job['gaps'] or 'none listed'}"]
        if show_first_seen:
            lines.insert(1, f"- Rating: {job['rating']} · first seen {row['first_seen']}")
        if note:
            lines.append(f"- Note: {note}")
        return "\n".join(lines) + "\n"

    def out_of_field(item):
        """True for an out-of-field role (column 'field' = out in screened.csv)."""
        job = item[0]
        return bool(job) and job.get("field", "").strip() == "out"

    def sorted_items(items, by_rating=False):
        """In-field jobs first, then out-of-field; high priority first in each."""
        rank = {"strong": 0, "possible": 1}
        return sorted(items, key=lambda i: (out_of_field(i), order(i[1]["company"]),
                                            rank.get(i[1]["rating"], 2) if by_rating else 0))

    def add_jobs(items, make_line, by_rating=False):
        """Add sorted jobs, with a divider before the first out-of-field one."""
        divider_done = False
        for item in sorted_items(items, by_rating):
            if out_of_field(item) and not divider_done:
                out.append("**Out-of-field roles (industry data-science experience):**\n")
                divider_done = True
            out.append(make_line(*item))

    new = sections["new"]
    out = [f"# Weekly job digest: {day}\n",
           f"Jobs fetched: {counts['fetched']} · rated after read posting: {counts['rated']} · "
           f"new strong: {sum(1 for j, _, _ in new if j['rating'] == 'strong')} · "
           f"new possible: {sum(1 for j, _, _ in new if j['rating'] == 'possible')} · "
           f"still open: {len(sections['still_open'])} · closed: {len(sections['closed'])}\n",
           "Every job below (except closed ones) was opened and confirmed live by read posting "
           "in this run. "
           "★ = high-priority company.\n"]

    # Failed steps first, so they are never missed
    if problems:
        out.append("## Problems in this run\n")
        out += [f"- {p}" for p in problems]
        out.append("")

    for title, rating in (("New strong matches", "strong"), ("New possible matches", "possible")):
        out.append(f"## {title}\n")
        items = [i for i in new if i[0]["rating"] == rating]
        if not items:
            out.append("None this week.\n")
        add_jobs(items, lambda job, row, note: job_block(job, row, note, show_first_seen=False))

    out.append("## Still open from earlier weeks\n")
    if not sections["still_open"]:
        out.append("None.\n")
    add_jobs(sections["still_open"],
             lambda job, row, note: job_block(job, row, note, show_first_seen=True),
             by_rating=True)

    out.append("## Closed since last week\n")
    if not sections["closed"]:
        out.append("None.\n")
    for _, row, _ in sorted_items(sections["closed"], by_rating=True):
        out.append(f"- {row['company']}{star(row['company'])}: {row['title']} "
                   f"({row['rating']}, first seen {row['first_seen']}, "
                   f"last seen {row['last_seen']}) {row['url']}")
    out.append("")

    out.append("## Check manually\n")
    for name in sorted(check, key=order):
        url, reason = check[name]
        out.append(f"- {name}{star(name)}: {reason}. {url}")
    out.append("")

    # LinkedIn alert jobs not found on the company's site (no link: we
    # never open linkedin.com, so these are not confirmed live)
    out.append("## LinkedIn only: check manually\n")
    if not linkedin:
        out.append("None.\n")
    for r in sorted(linkedin, key=lambda r: order(r["company"])):
        out.append(f"- {r['company']}{star(r['company'])}: {r['listed_title']} "
                   f"({r['reason']}). Search for it on LinkedIn yourself.")
    out.append("")

    out.append("## No careers page: consider direct outreach\n")
    for c in sorted(no_page, key=lambda c: order(c["name"])):
        site = c["website"] or c.get("israel_evidence", "") or "no website"
        out.append(f"- {c['name']}{star(c['name'])} ({c['category']}): {site}")
    out.append("")

    out.append("## Long-open\n")
    if not sections["long_open"]:
        out.append("None.\n")
    add_jobs(sections["long_open"],
             lambda job, row, _: f"- **{job['company']}{star(job['company'])}**: {job['title']} "
                                 f"({job['rating']}, open since {row['first_seen']}) {job['url']}",
             by_rating=True)
    out.append("")

    out.append("## New this week (companies)\n")
    if not added:
        out.append("None.\n")
    for c in sorted(added, key=lambda c: order(c["name"])):
        site = c.get("website") or c.get("source_link") or ""
        out.append(f"- {c['name']}{star(c['name'])} ({c['category']}; source: {c['source']}, "
                   f"{c['date_added']}): {c['reason']} {site}".rstrip())
    return "\n".join(out).rstrip() + "\n"


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    day = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

    screened_path = JOBS_DIR / f"{day}_screened.csv"
    problems = run_problems(day)
    # Without screened jobs the digest is still written (company sections and
    # problems), but the tracker is NOT updated: otherwise every open job
    # would wrongly be marked closed.
    screening_ok = screened_path.exists()
    if not screening_ok:
        print(f"Missing {screened_path}: job sections left empty, tracker not updated.")
        problems.append(f"digest: no screened jobs ({screened_path.name} missing), so the job "
                        "sections are empty and jobs_seen.csv was not updated")
    screened = [r for r in read_csv(screened_path) if r["url"]]
    raw_path = JOBS_DIR / f"{day}_raw.json"
    fetched = len(json.loads(raw_path.read_text(encoding="utf-8"))) if raw_path.exists() else 0

    companies = read_csv(COMPANIES)
    priority = {c["name"]: c.get("priority", "").strip().lower() for c in companies}

    tracker = load_tracker(day)
    # URLs used by more than one job (this run or in the tracker)
    url_counts = Counter(r["url"] for r in screened)
    tracker_counts = Counter(r["url"] for r in tracker)
    shared_urls = {u for u in set(url_counts) | set(tracker_counts)
                   if url_counts[u] > 1 or tracker_counts[u] > 1}
    ident = make_ident(shared_urls)
    live_jobs = {ident(r["url"], r["title"]): r for r in screened}

    if screening_ok:
        sections = update_tracker(day, tracker, live_jobs, ident)
    else:
        sections = {"new": [], "still_open": [], "long_open": [], "closed": []}
    check, no_page = company_lists(day, companies)
    text = build_digest(day, sections, check, no_page, priority,
                        {"fetched": fetched, "rated": len(live_jobs)},
                        linkedin_only_jobs(day), problems, new_companies(day, companies))

    # Same check as the hook: refuse to save a digest whose links were not
    # confirmed live by read posting in this run
    problems = validate(text, day)
    if problems:
        print("Digest NOT saved: some jobs lack a link confirmed by read posting in this run:")
        for p in problems:
            print("  - " + p)
        sys.exit(1)

    DIGESTS_DIR.mkdir(exist_ok=True)
    out_path = DIGESTS_DIR / f"{day}.md"
    out_path.write_text(text, encoding="utf-8")
    if screening_ok:
        write_tracker(TRACKER, tracker)
    print(f"New: {len(sections['new'])}, still open: {len(sections['still_open'])}, "
          f"closed: {len(sections['closed'])}, long-open: {len(sections['long_open'])}")
    tracker_note = f"updated {TRACKER} ({len(tracker)} jobs tracked)" if screening_ok \
        else "tracker NOT updated"
    print(f"Saved {out_path} and {tracker_note}")

    # Word copy, made from the saved .md (which passed the link check above).
    # A failure here leaves the .md and tracker as saved, but is reported
    # and gives exit code 1, so the /job-digest run logs it.
    try:
        from digest_docx import save_docx  # needs python-docx (requirements.txt)
        print(f"Saved {save_docx(out_path)}")
    except Exception as error:  # report any problem clearly
        print(f"DOCX NOT saved: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
