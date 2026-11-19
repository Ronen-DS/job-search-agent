# Helper script for the job-screening pipeline:
#   stage 1 title filter -> stage 2 read posting -> stage 3 profile match.
# The judging (stages 1 and 3) is done by the job-screener subagent; this
# script does the fetching and file handling around it.
#
# Steps (the date defaults to today):
#   titles : jobs/<date>_raw.json -> jobs/<date>_titles.csv (id, company, title)
#            The screener's title filter writes jobs/<date>_title_filter.json.
#   read   : read posting. Opens every "candidate" from
#            jobs/<date>_title_filter.json, checks it is live and in Israel,
#            and saves:
#              jobs/<date>_read_posting.json (every candidate, with status and reason)
#              jobs/<date>_to_screen.json    (only the live ones, for the screener)
#            The screener's profile match writes jobs/<date>_ratings.json.
#   merge  : jobs/<date>_to_screen.json + jobs/<date>_ratings.json
#              -> jobs/<date>_screened.csv
#
# Usage (run from the project root):
#   python .claude/skills/read-posting/read_posting.py titles [YYYY-MM-DD]
#   python .claude/skills/read-posting/read_posting.py read   [YYYY-MM-DD]
#   python .claude/skills/read-posting/read_posting.py merge  [YYYY-MM-DD]

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

# Reuse the feeds, location rules and render() from the fetch-jobs skill
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "skills" / "fetch-jobs"))
import fetch_jobs as fj  # noqa: E402  (import after the path change)

JOBS_DIR = fj.JOBS_DIR
DESCRIPTION_LIMIT = 3000  # characters of description kept per job

# Phrases that mean a job page is closed (lower case; Hebrew included)
CLOSED_PHRASES = [
    "no longer available", "no longer accepting", "no longer open",
    "position has been filled", "job not found", "page not found",
    "this job is closed", "this position is closed", "job has expired",
    "posting has been closed", "job you are looking for",
    "המשרה אינה זמינה", "המשרה נסגרה", "המשרה לא נמצאה", "משרה זו אינה",
    "הדף לא נמצא", "המשרה אינה פעילה",
    # AllJobs keeps closed jobs online with this line ("marked by the
    # employer as no longer current")
    "כלא אקטואלית", "אינה אקטואלית",
]


def file_for(day, kind, ext):
    return JOBS_DIR / f"{day}_{kind}.{ext}"


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_companies():
    with open(fj.COMPANIES_CSV, encoding="utf-8", newline="") as f:
        return {row["name"].strip(): row for row in csv.DictReader(f)}


def location_label(text):
    """The value after a 'Location:' label on a job page, e.g.
    'Location: IL', 'Location:\\nNew York, NY' or AllJobs' 'מיקום המשרה: תל אביב'.
    Returns '' if none."""
    match = re.search(r"(?:location|מיקום(?: המשרה)?)\s*:\s*\n?\s*([^\n]{1,60})", text, re.I)
    return match.group(1).strip() if match else ""


def status_from_label(value):
    """'israel', 'foreign' or 'unclear' for a Location label value."""
    if value.upper() in ("IL", "ISR", "ISRAEL"):
        return "israel"
    return fj.classify_location(value)


def israel_in_text(text):
    """True if the page text names an Israeli place. Place names only,
    not short codes like 'us', which also appear in 'contact us'.
    A foreign place in free text is NOT used to drop a job: company blurbs
    often mention other countries ('clinicians across the United States')."""
    return fj.contains_word(text.lower(), fj.ISRAEL_WORDS)


# ---------------------------------------------------------------------------
# Step 1: titles
# ---------------------------------------------------------------------------
def step_titles(day):
    raw = load_json(file_for(day, "raw", "json"))
    out_path = file_for(day, "titles", "csv")
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "company", "title"])
        for job_id, job in enumerate(raw):
            writer.writerow([job_id, job["company"], job["title"]])
    print(f"Wrote {len(raw)} titles for the title filter to {out_path}")


# ---------------------------------------------------------------------------
# Step 2: read posting
# ---------------------------------------------------------------------------
def dropped(reason):
    return {"status": "dropped", "reason": reason}


def read_feed_posting(job, company, feed_cache, israel_only):
    """Feed jobs: fetch the company's feed again (once per company) and
    check the job is still in it."""
    name = job["company"]
    if name not in feed_cache:
        ats = company["ats"].strip()
        feed_cache[name] = fj.FEEDS[ats](company["ats_board_id"].strip(),
                                         company["careers_url"].strip())
    wanted = fj.normalize_url(job["url"])
    match = next((j for j in feed_cache[name] if fj.normalize_url(j["url"]) == wanted), None)
    if not match:
        return dropped("closed: no longer in the company's job feed")

    location = match["location"]
    if match["remote"] and "remote" not in location.lower():
        location = f"{location} (Remote)" if location else "Remote"
    if israel_only:
        status, location = "israel", location or "Israel"
    else:
        status = fj.classify_location(location, match["title"], match["url"])
    if status == "foreign":
        return dropped(f"not in Israel: {location}")
    return {
        "status": "live",
        "reason": "still in the job feed",
        "page_title": match["title"],
        "location": location or "unclear",
        "location_status": status,
        "description": match["description"][:DESCRIPTION_LIMIT],
    }


def read_page_posting(job, company, israel_only, shared_page):
    """Rendered jobs: open the job page and check it still shows the job."""
    page = fj.render(job["url"])
    if page["blocked"]:
        return dropped("blocked by bot protection (not bypassed)")
    text = page["text"]
    low = text.lower()
    page_title_low = page["title"].lower()
    if len(text.strip()) < 100:
        return dropped("unreachable: page is empty")
    if "404" in page_title_low or "not found" in page_title_low or \
            any(p in low for p in CLOSED_PHRASES):
        return dropped("closed: page says the job is no longer available")
    # Jobs listed on a shared page (no page of their own): the title must
    # still be on that page
    if shared_page and job["title"] not in text:
        return dropped("closed: title no longer on the page")

    if israel_only:
        status, location = "israel", job["location"] or "Israel"
    else:
        status = fj.classify_location(job["location"], job["title"], job["url"])
        location = job["location"]
        if status == "unclear":
            # 1. A "Location:" label on the job page is the best evidence
            label = location_label(text)
            if label:
                status = status_from_label(label)
                location = label
            # 2. Otherwise an Israeli place anywhere on the page
            if status == "unclear" and israel_in_text(text):
                status = "israel"
                location = location or "Israel (from job page)"
            location = location or "unclear"
    if status == "foreign":
        return dropped(f"not in Israel (job page): {location}")

    return {
        "status": "live",
        "reason": "job page open",
        "page_title": page["title"],
        "location": location,
        "location_status": status,
        "description": text[:DESCRIPTION_LIMIT],
    }


def linkedin_only(reason):
    """A LinkedIn alert job not found on the company's own site: listed in
    the digest under 'LinkedIn only: check manually'."""
    return {"status": "linkedin_only", "reason": reason}


def same_title(a, b):
    """True if two job titles are the same job: equal after removing
    punctuation, or one contains the other (for longer titles)."""
    a = re.sub(r"[^\w]+", " ", a.lower()).strip()
    b = re.sub(r"[^\w]+", " ", b.lower()).strip()
    if a == b:
        return True
    return min(len(a), len(b)) >= 8 and (a in b or b in a)


def find_on_company_site(job, company, feed_cache, settings):
    """LinkedIn jobs come with company and title only (linkedin.com is never
    opened). Look for the same title in the company's own feed or careers
    page. Returns (found_job or None, reason)."""
    if not company:
        return None, "company not in companies.csv, so no careers page to check"
    name = job["company"]
    ats = company.get("ats", "").strip()
    board = company.get("ats_board_id", "").strip()
    careers_url = company.get("careers_url", "").strip()
    try:
        if ats in fj.FEEDS and board:
            if name not in feed_cache:
                feed_cache[name] = fj.FEEDS[ats](board, careers_url)
            site_jobs, source = feed_cache[name], "feed"
        elif ats in fj.RENDER_ATS and careers_url:
            site_jobs, source = fj.render_jobs(careers_url, settings.get(name, {})), "render"
        else:
            return None, f"careers page can't be read automatically (ats {ats or 'unknown'})"
    except Exception as error:  # report the problem and move on
        return None, f"careers page could not be read: {str(error)[:120]}"
    match = next((j for j in site_jobs if same_title(j["title"], job["title"])), None)
    if not match:
        return None, "not found on the company's careers page"
    found = dict(job)
    found.update({"url": match["url"], "title": match["title"],
                  "location": match.get("location") or job["location"], "source": source})
    return found, ""


def step_read_posting(day):
    raw = load_json(file_for(day, "raw", "json"))
    title_filter = load_json(file_for(day, "title_filter", "json"))
    companies = load_companies()

    candidate_ids = [int(t["id"]) for t in title_filter if t.get("decision") == "candidate"]
    # Also re-check jobs still open from earlier weeks (jobs_seen.csv), so
    # the digest's "Still open" jobs are read again in this run too
    tracker_path = PROJECT_ROOT / "jobs_seen.csv"
    if tracker_path.exists():
        with open(tracker_path, encoding="utf-8-sig", newline="") as f:
            open_urls = {r["url"] for r in csv.DictReader(f) if r["status"] == "open"}
        already = set(candidate_ids)
        extra = [i for i, j in enumerate(raw) if j["url"] in open_urls and i not in already]
        if extra:
            print(f"Also re-checking {len(extra)} jobs still open from earlier weeks")
        candidate_ids += extra
    # A link used by more than one job is a shared page (e.g. a list of
    # jobs with no separate job pages)
    link_counts = Counter(j["url"] for j in raw if j["url"])
    candidate_urls = {fj.normalize_url(raw[i]["url"]) for i in candidate_ids if raw[i]["url"]}
    settings = {}
    if fj.SETTINGS_FILE.exists():
        settings = load_json(fj.SETTINGS_FILE)

    feed_cache = {}
    results = []
    for number, job_id in enumerate(candidate_ids):
        job = raw[job_id]
        company = companies.get(job["company"], {})
        israel_only = company.get("type", "").strip() in fj.ISRAEL_ONLY_TYPES
        print(f"[{number + 1}/{len(candidate_ids)}] {job['company']}: {job['title'][:70]}")
        if number > 0:
            time.sleep(fj.PAUSE_SECONDS)  # be polite to the sites

        try:
            if job.get("linkedin_only"):
                # LinkedIn alert: look for the job on the company's own site
                found, reason = find_on_company_site(job, company, feed_cache, settings)
                if found is None:
                    result = linkedin_only(reason)
                elif fj.normalize_url(found["url"]) in candidate_urls:
                    result = dropped("duplicate: already fetched from the company's careers page")
                else:
                    job = found
                    candidate_urls.add(fj.normalize_url(job["url"]))
                    time.sleep(fj.PAUSE_SECONDS)
                    if job["source"] == "feed":
                        result = read_feed_posting(job, company, feed_cache, israel_only)
                    else:
                        shared = fj.normalize_url(job["url"]) == \
                            fj.normalize_url(company.get("careers_url", ""))
                        result = read_page_posting(job, company, israel_only, shared)
                    result["reason"] += " (LinkedIn alert job, found on the company's site)"
            elif not job["url"]:
                result = dropped("no link")
            elif fj.is_skipped_domain(job["url"]):
                result = dropped("link is on a site we never fetch")
            elif job["source"] == "feed" and company.get("ats", "").strip() in fj.FEEDS:
                result = read_feed_posting(job, company, feed_cache, israel_only)
            else:
                shared = link_counts[job["url"]] > 1 or \
                    fj.normalize_url(job["url"]) == fj.normalize_url(company.get("careers_url", ""))
                result = read_page_posting(job, company, israel_only, shared)
        except Exception as error:  # report the problem and move on
            result = dropped(f"unreachable: {str(error)[:150]}")

        result.update({
            "id": job_id,
            "company": job["company"],
            "listed_title": job["title"],
            "url": job["url"],
            "source": job["source"],
        })
        results.append(result)

    save_json(file_for(day, "read_posting", "json"), results)
    live = [
        {k: r[k] for k in ("id", "company", "listed_title", "page_title",
                           "location", "location_status", "url", "description")}
        for r in results if r["status"] == "live"
    ]
    save_json(file_for(day, "to_screen", "json"), live)

    # Summary
    linkedin_count = sum(1 for r in results if r["status"] == "linkedin_only")
    print(f"\nRead posting: {len(results)} candidates, live: {len(live)}, "
          f"LinkedIn only (check manually): {linkedin_count}, "
          f"dropped: {len(results) - len(live) - linkedin_count}")
    reasons = Counter(r["reason"].split(":")[0] for r in results if r["status"] == "dropped")
    for reason, count in reasons.most_common():
        print(f"  dropped ({reason}): {count}")
    print(f"Saved {file_for(day, 'read_posting', 'json')} and {file_for(day, 'to_screen', 'json')}")


# ---------------------------------------------------------------------------
# Step 3: merge
# ---------------------------------------------------------------------------
def step_merge(day):
    jobs = {str(j["id"]): j for j in load_json(file_for(day, "to_screen", "json"))}
    ratings = load_json(file_for(day, "ratings", "json"))

    rows = []
    rated = set()
    for r in ratings:
        job = jobs.get(str(r["id"]))
        if not job:
            print(f"Warning: rating for unknown id {r['id']} ignored")
            continue
        rated.add(str(r["id"]))
        # in_field: false = out-of-field role (e.g. a data-science job in
        # fintech). Missing = in field (ratings made before this field existed).
        in_field = r.get("in_field", True) is not False
        rating = r.get("rating", "")
        if not in_field and rating == "strong":
            # Out-of-field roles are "possible" at most (profile.md, Rating rules)
            print(f"Note: job id {r['id']} is out of field: rating capped at possible")
            rating = "possible"
        rows.append([
            job["company"],
            r.get("title") or job["listed_title"],
            job["location"],
            job["url"],
            rating,
            r.get("reason", ""),
            "; ".join(r.get("matched_skills", [])),
            "; ".join(r.get("gaps", [])),
            "in" if in_field else "out",
        ])
    for missing in sorted(set(jobs) - rated, key=int):
        print(f"Warning: job id {missing} has no rating")

    # Strong first, then possible, then skip; in-field before out-of-field
    order = {"strong": 0, "possible": 1, "skip": 2}
    rows.sort(key=lambda row: (order.get(row[4], 3), row[8] == "out", row[0]))

    out_path = file_for(day, "screened", "csv")
    # utf-8-sig lets Excel show Hebrew correctly
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["company", "title", "location", "url", "rating",
                         "reason", "matched_skills", "gaps", "field"])
        writer.writerows(rows)

    counts = Counter(row[4] for row in rows)
    out_of_field = sum(1 for row in rows if row[8] == "out" and row[4] != "skip")
    print(f"Profile match: strong: {counts['strong']}, possible: {counts['possible']}, "
          f"skip: {counts['skip']} (out-of-field among strong/possible: {out_of_field})")
    print(f"Saved {len(rows)} rated jobs to {out_path}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Job pipeline helper: titles for the title filter, read posting, merge")
    parser.add_argument("step", choices=["titles", "read", "merge"])
    parser.add_argument("day", nargs="?", default=date.today().isoformat(),
                        help="date of the raw file, YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    {"titles": step_titles, "read": step_read_posting, "merge": step_merge}[args.step](args.day)


if __name__ == "__main__":
    main()
