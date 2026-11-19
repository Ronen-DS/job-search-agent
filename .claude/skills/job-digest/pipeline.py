# Helper for the /job-digest command (the full weekly pipeline).
#
#   begin     : start a run for today. Moves today's earlier pipeline files
#               (titles, title filter, read posting, ratings, screened,
#               digest, run log) to jobs/archive/<date>_<time>/ so a failed
#               step can never reuse stale results. Nothing is deleted.
#               Keeps raw.json (fetch-jobs keeps email jobs in it),
#               email_jobs.json and jobs_seen_before.csv (the tracker
#               snapshot that makes a same-day re-run give the same result).
#   unmapped  : companies in companies.csv with no careers details yet
#               (ats empty or "unknown"), for the company-mapper agent.
#   log       : record how a step went in jobs/<date>_run_log.json:
#               log <step> <ok|failed|partial|skipped> "<detail>"
#   report    : the end-of-run report: jobs per source, counts after each
#               stage, new strong / possible matches, companies added, errors.
#
# Usage (from the project root):
#   python .claude/skills/job-digest/pipeline.py begin
#   python .claude/skills/job-digest/pipeline.py unmapped
#   python .claude/skills/job-digest/pipeline.py log fetch-jobs ok "79 companies"
#   python .claude/skills/job-digest/pipeline.py report

import argparse
import csv
import json
import shutil
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
JOBS_DIR = PROJECT_ROOT / "jobs"
DIGESTS_DIR = PROJECT_ROOT / "digests"
COMPANIES_CSV = PROJECT_ROOT / "companies.csv"

# Today's pipeline outputs that a new run must recreate
STAGE_FILES = ["titles.csv", "title_filter.json", "read_posting.json",
               "to_screen.json", "ratings.json", "screened.csv", "run_log.json"]
STATUSES = ["ok", "failed", "partial", "skipped"]


def today():
    return date.today().isoformat()


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_csv(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
def step_begin(day):
    archive = JOBS_DIR / "archive" / f"{day}_{datetime.now():%H%M%S}"
    moved = []
    for name in STAGE_FILES:
        path = JOBS_DIR / f"{day}_{name}"
        if path.exists():
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), archive / path.name)
            moved.append(path.name)
    digest = DIGESTS_DIR / f"{day}.md"
    if digest.exists():
        archive.mkdir(parents=True, exist_ok=True)
        shutil.copy(digest, archive / f"digest_{day}.md")  # copy: replaced in step 5
        moved.append(f"digests/{day}.md (copied)")
    # Snapshot of the company names, so the report can show what this run added
    names = [c["name"] for c in read_csv(COMPANIES_CSV)]
    JOBS_DIR.mkdir(exist_ok=True)
    with open(JOBS_DIR / f"{day}_companies_at_start.json", "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False)
    if moved:
        print(f"Earlier files for {day} moved to {archive}:")
        for name in moved:
            print("  " + name)
    else:
        print(f"No earlier pipeline files for {day}.")
    print(f"Run started for {day}.")


def step_unmapped():
    rows = [c for c in read_csv(COMPANIES_CSV)
            if c.get("ats", "").strip() in ("", "unknown")]
    if not rows:
        print("All companies have careers details. Nothing to map.")
        return
    print(f"{len(rows)} companies need mapping:")
    for c in rows:
        print(f"  {c['name']} (ats: {c.get('ats', '').strip() or 'empty'})")
    print("Names for the company-mapper: " + ", ".join(c["name"] for c in rows))


def step_log(day, step, status, detail):
    if status not in STATUSES:
        sys.exit(f"status must be one of {STATUSES}")
    path = JOBS_DIR / f"{day}_run_log.json"
    log = load_json(path, [])
    log.append({"step": step, "status": status, "detail": detail,
                "time": datetime.now().isoformat(timespec="seconds")})
    JOBS_DIR.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print(f"Logged: {step}: {status}")


# ---------------------------------------------------------------------------
def digest_jobs(text, section):
    """'### company: title' headings in one digest section."""
    jobs, current = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
        elif current == section and line.startswith("### "):
            jobs.append(line[4:].strip())
    return jobs


def step_report(day):
    out = [f"# Job-digest report: {day}", ""]

    # Jobs per source
    raw = load_json(JOBS_DIR / f"{day}_raw.json")
    out.append("## Jobs per source")
    if raw is None:
        out.append("- No raw job file (fetch-jobs and read-alerts both failed?)")
    else:
        names = {"feed": "hiring-system feeds", "render": "careers pages",
                 "email": "alert emails"}
        for source, n in Counter(j["source"] for j in raw).most_common():
            out.append(f"- {names.get(source, source)}: {n}")
        linkedin = sum(1 for j in raw if j.get("linkedin_only"))
        if linkedin:
            out.append(f"  (of the email jobs, {linkedin} from LinkedIn: company + title only)")
    out.append("")

    # Counts after each stage
    out.append("## Counts after each stage")
    if raw is not None:
        out.append(f"1. Fetched (raw): {len(raw)}")
    title_filter = load_json(JOBS_DIR / f"{day}_title_filter.json")
    if title_filter is not None:
        decisions = Counter(t.get("decision") for t in title_filter)
        out.append(f"2. Title filter: {decisions['candidate']} candidates, {decisions['drop']} dropped")
    else:
        out.append("2. Title filter: no output")
    read_posting = load_json(JOBS_DIR / f"{day}_read_posting.json")
    if read_posting is not None:
        statuses = Counter(r["status"] for r in read_posting)
        out.append(f"3. Read posting: {len(read_posting)} opened, {statuses['live']} live, "
                   f"{statuses['linkedin_only']} LinkedIn only, {statuses['dropped']} dropped")
        reasons = Counter(r["reason"].split(":")[0] for r in read_posting
                          if r["status"] == "dropped")
        if reasons:
            out.append("   dropped: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common()))
    else:
        out.append("3. Read posting: no output")
    ratings = load_json(JOBS_DIR / f"{day}_ratings.json")
    if ratings is not None:
        r = Counter(x.get("rating") for x in ratings)
        out.append(f"4. Profile match: {r['strong']} strong, {r['possible']} possible, {r['skip']} skip")
    else:
        out.append("4. Profile match: no output")
    digest_path = DIGESTS_DIR / f"{day}.md"
    digest = digest_path.read_text(encoding="utf-8") if digest_path.exists() else None
    if digest is not None:
        strong = digest_jobs(digest, "New strong matches")
        possible = digest_jobs(digest, "New possible matches")
        still = digest_jobs(digest, "Still open from earlier weeks")
        out.append(f"5. Digest: {len(strong)} new strong, {len(possible)} new possible, "
                   f"{len(still)} still open")
    else:
        out.append("5. Digest: NOT written")
    out.append("")

    # New matches
    if digest is not None:
        for title, jobs in (("New strong matches", strong), ("New possible matches", possible)):
            out.append(f"## {title}")
            out += [f"- {j}" for j in jobs] or ["- none"]
            out.append("")

    # Companies added in this run: not in the snapshot taken by 'begin'.
    # Without a snapshot: added today by the pipeline (not the user's own list).
    companies = read_csv(COMPANIES_CSV)
    at_start = load_json(JOBS_DIR / f"{day}_companies_at_start.json")
    if at_start is not None:
        added = [c for c in companies if c["name"] not in set(at_start)]
    else:
        added = [c for c in companies
                 if c.get("date_added") == day and c.get("source") != "user"]
    out.append("## Companies added in this run")
    out += [f"- {c['name']} ({c['category']}; source: {c['source']})" for c in added] or ["- none"]
    out.append("")

    # Errors
    out.append("## Problems")
    problems = []
    for entry in load_json(JOBS_DIR / f"{day}_run_log.json", []):
        if entry["status"] != "ok":
            problems.append(f"{entry['step']}: {entry['status']}: {entry['detail']}")
    fetch_log = load_json(JOBS_DIR / f"{day}_fetch_log.json", [])
    errors = [e for e in fetch_log if e["method"] == "error"]
    for e in errors:
        problems.append(f"fetch error: {e['company']}: {e['note'][:120]}")
    out += [f"- {p}" for p in problems] or ["- none"]
    if digest_path.exists():
        out += ["", f"Digest: {digest_path.relative_to(PROJECT_ROOT)}"]
    print("\n".join(out))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="/job-digest helper")
    parser.add_argument("step", choices=["begin", "unmapped", "log", "report"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--date", default=today(), help="YYYY-MM-DD (default: today)")
    a = parser.parse_args()
    if a.step == "begin":
        step_begin(a.date)
    elif a.step == "unmapped":
        step_unmapped()
    elif a.step == "log":
        if len(a.args) < 2:
            sys.exit('usage: log <step> <status> "<detail>"')
        step_log(a.date, a.args[0], a.args[1], " ".join(a.args[2:]))
    else:
        step_report(a.date)


if __name__ == "__main__":
    main()
