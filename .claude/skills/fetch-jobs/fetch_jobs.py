# Fetches open jobs for the companies in companies.csv and saves them to
# jobs/<date>_raw.json.
#
# How each company is handled depends on its "ats" column:
#   greenhouse, lever, ashby, workable, recruitee, smartrecruiters
#       -> the hiring system's public job feed (JSON), using ats_board_id
#   comeet
#       -> rendered careers page (Comeet's feed needs a private company token)
#   own_page, js_page, eightfold, redmatch, workday
#       -> rendered careers page (scripts/render_page.py), then job links are
#          picked out of the page
#   none, blocked (and anything else, e.g. unknown)
#       -> skipped
# If a feed fails (or a feed company has no board ID), the careers page is
# rendered instead.
#
# Location rules:
#   - academia and hospital companies: every job counts as Israel
#   - an Israeli place is named (also "Remote - Israel")  -> kept, "israel"
#   - another country, city or region is named (also "Remote US", "EMEA",
#     "Worldwide")                                        -> dropped
#   - no place named at all (also plain "Remote")          -> kept, "unclear"
#
# Optional per-company settings (extra pages, plain-text job lists, filters
# for shared portals) are in company_settings.json next to this script.
# A job (same link and title) already saved for an earlier company is
# skipped as a duplicate, e.g. when several companies share one portal.
#
# Usage (run from the project root):
#   python .claude/skills/fetch-jobs/fetch_jobs.py
#   python .claude/skills/fetch-jobs/fetch_jobs.py --companies "Immunai,Protai"

import argparse
import csv
import html
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

# This file lives in <project>/.claude/skills/fetch-jobs/, so the project
# root is three folders up. We add scripts/ to Python's search path so we can
# reuse the render() function from scripts/render_page.py.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from render_page import render  # noqa: E402  (import after the path change)

COMPANIES_CSV = PROJECT_ROOT / "companies.csv"
SETTINGS_FILE = Path(__file__).parent / "company_settings.json"
JOBS_DIR = PROJECT_ROOT / "jobs"
PAUSE_SECONDS = 2  # wait between requests, to be polite to the sites
HEADERS = {"User-Agent": "personal-job-search-script/1.0"}

FEED_ATS = {"greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters"}
RENDER_ATS = {"comeet", "own_page", "js_page", "eightfold", "redmatch", "workday"}
# Company types that only exist in Israel: their jobs count as Israel
ISRAEL_ONLY_TYPES = {"academia", "hospital"}

# Never fetch these sites, and ignore links pointing to them
SKIP_DOMAINS = ["linkedin.com", "facebook.com", "twitter.com", "x.com",
                "instagram.com", "youtube.com", "glassdoor.com"]

# ---------------------------------------------------------------------------
# Location keywords (lower case). Hebrew words are included.
# ---------------------------------------------------------------------------
ISRAEL_WORDS = [
    "israel", "tel aviv", "tel-aviv", "tel aviv-yafo", "rehovot", "haifa",
    "jerusalem", "herzliya", "herzeliya", "petah tikva", "petach tikva",
    "petah tiqva", "ness ziona", "nes ziona", "kfar saba", "netanya",
    "ramat gan", "caesarea", "beer sheva", "be'er sheva", "beersheba",
    "rosh haayin", "rosh ha'ayin", "yokneam", "yoqneam", "modiin", "raanana",
    "ra'anana", "hod hasharon", "holon", "rishon lezion", "airport city",
    "or yehuda", "lod", "ramla", "givatayim", "bnei brak", "ashkelon",
    "ashdod", "afula", "eilat", "nazareth", "tiberias", "safed", "hadera",
    "karmiel", "nahariya", "bat yam", "kiryat", "gush dan",
    "ישראל", "תל אביב", 'ת"א', "רחובות", "חיפה", "ירושלים", "הרצליה",
    "פתח תקווה", "פתח תקוה", "נס ציונה", "כפר סבא", "נתניה", "רמת גן",
    "קיסריה", "באר שבע", "ראש העין", "יקנעם", "רעננה", "מודיעין", "חולון",
    "ראשון לציון", "אשקלון", "אשדוד", "עפולה", "אילת", "נצרת", "טבריה",
    "צפת", "חדרה", "כרמיאל", "נהריה", "בת ים", "קריית", "קרית", "גוש דן",
    "השפלה", "השרון", "הגליל", "הנגב", "העמק", "מחוז", "רמלה",
    "גבעתיים", "בני ברק", "קריית שמונה", "דימונה",
    "בית שמש", "מעלה אדומים", "שדרות", "אופקים", "נתיבות",
    # Very short names that also occur inside other Hebrew words
    # (e.g. "לוד" in "ילודים") are left out on purpose.
]
FOREIGN_WORDS = [
    "united states", "usa", "u.s.", "canada", "united kingdom", "uk",
    "england", "london", "germany", "berlin", "munich", "france", "paris",
    "netherlands", "amsterdam", "switzerland", "spain", "italy", "poland",
    "india", "bangalore", "china", "shanghai", "japan", "singapore",
    "australia", "boston", "new york", "san francisco", "california",
    "massachusetts", "cambridge", "texas", "new jersey", "north carolina",
    "washington", "chicago", "seattle", "toronto", "ireland", "dublin",
    "belgium", "denmark", "sweden", "united arab emirates", "uae",
    "abu dhabi", "dubai", "brazil", "sao paulo", "mexico", "argentina",
    "korea", "taiwan", "hong kong", "south africa", "austria", "czech",
    "hungary", "portugal", "greece", "turkey", "san diego", "fremont",
    "philippines", "malaysia", "thailand", "vietnam", "indonesia",
    "new zealand", "norway", "finland",
]
# Regions that are not Israel-specific. Checked in the location field only,
# because words like "global" are common in job titles.
REGION_WORDS = ["emea", "europe", "worldwide", "global", "anywhere",
                "americas", "north america", "latam", "apac", "asia"]
# Short country codes, checked in the location field and the job address
# (e.g. Workday's /job/US---Remote/...), but not in titles
# ("join us" in a title is not a country).
LOCATION_CODES = ["us", "uk", "eu"]
REMOTE_WORDS = ["remote", "work from home", "מרחוק"]

# Link texts that are buttons, not job titles
GENERIC_TEXT = {
    "read more", "read more >", "view role", "view job", "apply", "apply now",
    "learn more", "more", "details", "see details", "view", "view details",
    "careers", "jobs", "open positions", "all jobs", "see all", "back",
    "home", "about", "about us", "contact", "contact us", "join us",
    "get in touch", "follow us", "join talent network", "talent network",
    "filters applied", "search for jobs", "clear all",
    "קרא עוד", "קראו עוד", "להגשת מועמדות", "הגש מועמדות", "פרטים נוספים",
    "למשרה", "משרות",
}

# Address patterns that usually point to a single job page
JOB_LINK_PATTERNS = [
    r"/jobs?/[^/?#]+",           # .../job/123, .../jobs/data-scientist
    r"/positions?/[^/?#]+",      # .../positions/123
    r"/o/[^/?#]+",               # Recruitee job pages
    r"[?&](comppositionid|positionid|jobid|job_id|gh_jid)=",  # RedMatch etc.
]
# .../careers/<something> is also often a job page, but it is weaker
# evidence, so for those we require real link text (not just "Read more")
# and skip common menu pages like .../careers/join or .../careers/benefits.
WEAK_JOB_LINK_PATTERN = r"/careers?/[^/?#]+"
MENU_SLUGS = {"join", "about", "benefits", "culture", "life", "faq", "faqs",
              "team", "students", "internships", "search", "apply", "login"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def contains_word(text, words):
    """True if any of the words appears in text as a whole word."""
    for word in words:
        # (?<![a-z]) and (?![a-z]) stop "lod" from matching inside "lodging"
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", text):
            return True
    return False


def classify_location(location, title="", url=""):
    """Return 'israel', 'foreign' or 'unclear' (see the rules at the top).
    Remote jobs follow the same rules: 'Remote - Israel' is israel,
    'Remote US' is foreign, plain 'Remote' is unclear."""
    loc = location.lower()
    # Place names in the job address also count, e.g. /job/kfar-saba/...
    path = re.sub(r"[-_/]+", " ", urlparse(url).path.lower())
    everything = " ".join([loc, title.lower(), path])
    if contains_word(everything, ISRAEL_WORDS):
        return "israel"
    if contains_word(everything, FOREIGN_WORDS):
        return "foreign"
    if contains_word(loc, REGION_WORDS) or contains_word(f"{loc} {path}", LOCATION_CODES):
        return "foreign"
    return "unclear"


def is_location_line(line):
    """A short line that names a place (or says remote)."""
    text = line.lower()
    return len(line) <= 60 and (
        contains_word(text, ISRAEL_WORDS)
        or contains_word(text, FOREIGN_WORDS)
        or contains_word(text, REMOTE_WORDS)
    )


def clean_html(raw):
    """Turn an HTML description into plain text."""
    if not raw:
        return ""
    text = html.unescape(raw)               # Greenhouse sends escaped HTML
    text = re.sub(r"<[^>]+>", " ", text)    # drop the tags
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def is_skipped_domain(url):
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in SKIP_DOMAINS)


def normalize_url(url):
    """Remove the #fragment and trailing slash, so equal pages compare equal."""
    return url.split("#")[0].rstrip("/")


def get_json(url):
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()  # turns HTTP errors (404, 500...) into exceptions
    return response.json()


# ---------------------------------------------------------------------------
# Public job feeds. Each returns a list of jobs as dictionaries with
# title, location, remote (True/False), url and description.
# Endpoints come from each system's own documentation.
# ---------------------------------------------------------------------------
def feed_greenhouse(board, careers_url):
    # https://docs.greenhouse.io/job-board.html
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
    return [
        {
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "remote": False,
            "url": j.get("absolute_url", ""),
            "description": clean_html(j.get("content")),
        }
        for j in data.get("jobs", [])
    ]


def feed_lever(board, careers_url):
    # https://github.com/lever/postings-api (EU accounts use api.eu.lever.co)
    host = "api.eu.lever.co" if "eu.lever.co" in careers_url else "api.lever.co"
    data = get_json(f"https://{host}/v0/postings/{board}?mode=json")
    return [
        {
            "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""),
            "remote": j.get("workplaceType") == "remote",
            "url": j.get("hostedUrl", ""),
            "description": j.get("descriptionPlain", ""),
        }
        for j in data
    ]


def feed_ashby(board, careers_url):
    # https://developers.ashbyhq.com/docs/public-job-posting-api
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    jobs = []
    for j in data.get("jobs", []):
        # isRemote is also true for hybrid jobs, so prefer workplaceType
        workplace = j.get("workplaceType")
        remote = workplace == "Remote" if workplace else bool(j.get("isRemote"))
        jobs.append({
            "title": j.get("title", ""),
            "location": j.get("location", ""),
            "remote": remote,
            "url": j.get("jobUrl", ""),
            "description": j.get("descriptionPlain", ""),
        })
    return jobs


def feed_workable(board, careers_url):
    # https://workable.readme.io/reference/jobs-1 (public, no key)
    data = get_json(f"https://www.workable.com/api/accounts/{board}?details=true")
    jobs = []
    for j in data.get("jobs", []):
        parts = [j.get("city"), j.get("state"), j.get("country")]
        jobs.append({
            "title": j.get("title", ""),
            "location": ", ".join(p for p in parts if p),
            "remote": bool(j.get("telecommuting")) or j.get("workplace_type") == "remote",
            "url": j.get("url") or j.get("shortlink", ""),
            "description": clean_html(j.get("description")),
        })
    return jobs


def feed_recruitee(board, careers_url):
    # https://docs.recruitee.com/reference/offers (public careers site API)
    data = get_json(f"https://{board}.recruitee.com/api/offers/")
    jobs = []
    for j in data.get("offers", []):
        places = []
        for loc in j.get("locations") or []:
            name = loc.get("name") or ", ".join(
                p for p in [loc.get("city"), loc.get("country")] if p
            )
            if name:
                places.append(name)
        jobs.append({
            "title": j.get("title", ""),
            "location": "; ".join(places),
            "remote": bool(j.get("remote")),
            "url": j.get("careers_url", ""),
            "description": clean_html(j.get("description")),
        })
    return jobs


def feed_smartrecruiters(board, careers_url):
    # https://developers.smartrecruiters.com/docs/endpoints (Posting API)
    # The list gives title and location only, so for each job that is not
    # clearly abroad we also open the single-posting endpoint to get its
    # public link (applyUrl) and description.
    base = f"https://api.smartrecruiters.com/v1/companies/{board}/postings"
    postings = []
    offset = 0
    while True:
        data = get_json(f"{base}?limit=100&offset={offset}")
        page = data.get("content", [])
        postings.extend(page)
        offset += len(page)
        if not page or offset >= data.get("totalFound", 0):
            break
        time.sleep(PAUSE_SECONDS)

    jobs = []
    for p in postings:
        loc = p.get("location") or {}
        parts = [loc.get("city"), loc.get("region"), loc.get("country")]
        location = ", ".join(x for x in parts if x)
        job = {
            "title": p.get("name", ""),
            "location": location,
            "remote": bool(loc.get("remote")),
            "url": "",
            "description": "",
        }
        if classify_location(location, job["title"]) != "foreign":
            time.sleep(PAUSE_SECONDS)
            detail = get_json(f"{base}/{p.get('id')}")
            job["url"] = detail.get("applyUrl", "")
            sections = (detail.get("jobAd") or {}).get("sections") or {}
            texts = [clean_html((s or {}).get("text")) for s in sections.values()]
            job["description"] = " ".join(t for t in texts if t)
        jobs.append(job)
    return jobs


FEEDS = {
    "greenhouse": feed_greenhouse,
    "lever": feed_lever,
    "ashby": feed_ashby,
    "workable": feed_workable,
    "recruitee": feed_recruitee,
    "smartrecruiters": feed_smartrecruiters,
}


# ---------------------------------------------------------------------------
# Rendered pages: pick job titles and links out of what render() returns
# ---------------------------------------------------------------------------
def title_from_url(url):
    """Make a title from the address, e.g. .../senior-data-scientist/25.471.
    We take the last part that contains a real word (3+ letters in a row),
    skipping ID codes like '25.471' or 'B2.D6E' (Comeet puts those last)."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    for part in reversed(parts):
        if re.search(r"[A-Za-z]{3,}", part):
            return re.sub(r"[-_]+", " ", part).strip().capitalize()
    return ""


def jobs_from_links(links, careers_url, final_url):
    """Find links that look like single job pages and build jobs from them."""
    page_urls = {normalize_url(careers_url), normalize_url(final_url)}

    # The same job is often linked several times (title, location,
    # "View role" button). Group the link texts by address first.
    texts_by_url = {}
    strong_by_url = {}
    for link in links:
        href = link["href"]
        if not href.startswith("http") or is_skipped_domain(href):
            continue
        if normalize_url(href) in page_urls:
            continue
        strong = any(re.search(p, href, re.I) for p in JOB_LINK_PATTERNS)
        weak = re.search(WEAK_JOB_LINK_PATTERN, href, re.I)
        last_part = urlparse(href).path.rstrip("/").split("/")[-1].lower()
        if weak and not strong and last_part in MENU_SLUGS:
            continue
        if not (strong or weak):
            continue
        key = href.split("#")[0]
        texts_by_url.setdefault(key, []).append(link["text"])
        strong_by_url[key] = strong_by_url.get(key, False) or strong

    jobs = []
    for url, texts in texts_by_url.items():
        # Split every link text into separate non-empty lines
        lines = [l.strip() for t in texts for l in t.splitlines() if l.strip()]
        useful = [l for l in lines if l.lower() not in GENERIC_TEXT]

        # Title: first line that is not a place and not too long. A line that
        # ends with a position number, e.g. "מזכיר/ה רפואי/ת (50371)" on
        # RedMatch portals, is the title for sure, so prefer it.
        title_options = [l for l in useful if not is_location_line(l) and len(l) <= 150]
        numbered = [l for l in title_options if re.search(r"\(\d{4,}\)\s*$", l)]
        title = (numbered or title_options or [""])[0]
        if not title and useful:
            title = useful[0][:150]
        if not title:
            if not strong_by_url[url]:
                continue  # weak link with no text: probably not a job
            title = title_from_url(url)

        location = next((l for l in useful if is_location_line(l)), "")
        long_lines = [l for l in useful if l != title and len(l) > 60]
        jobs.append({
            "title": title,
            "location": location,
            "remote": False,
            "url": url,
            "description": " ".join(long_lines),
            # All the card's text, used by job_filter (not saved)
            "context": "\n".join(lines),
        })
    return jobs


def jobs_from_text_list(text, page_url, start, end):
    """Jobs listed as plain lines between a 'start' line and an 'end' line,
    e.g. under the heading 'משרות פתוחות' (set in company_settings.json)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if start not in lines:
        return []
    jobs = []
    for line in lines[lines.index(start) + 1:]:
        if line == end:
            break
        jobs.append({
            "title": line,
            "location": "",
            "remote": False,
            "url": page_url,  # no separate job page link on the page
            "description": "",
        })
    return jobs


def jobs_from_text(text, page_url):
    """Last resort when jobs are not links: a short line followed by a
    place name (e.g. 'AI Engineer' / 'Ramat Gan') is taken as a job."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Start after an "Open positions" style heading if the page has one
    start = 0
    for i, line in enumerate(lines):
        if re.search(r"open positions|current openings|open roles|משרות פתוחות", line, re.I):
            start = i + 1
            break

    jobs = []
    seen = set()
    for i in range(start, len(lines) - 1):
        title, next_line = lines[i], lines[i + 1]
        if (
            is_location_line(next_line)
            and not next_line.lower().startswith("search")  # e.g. 'Search: "Israel"'
            and not is_location_line(title)
            and 3 <= len(title) <= 80
            and title.lower() not in GENERIC_TEXT
            and title not in seen
        ):
            seen.add(title)
            jobs.append({
                "title": title,
                "location": next_line,
                "remote": False,
                "url": page_url,  # no separate job page link on the page
                "description": "",
            })
    return jobs


def render_jobs(careers_url, settings):
    """Render the careers page plus any extra pages from company_settings.json
    and collect the jobs found on all of them."""
    pages = [careers_url] + settings.get("extra_pages", [])
    text_list = settings.get("text_list")
    jobs = []
    for number, url in enumerate(pages):
        if is_skipped_domain(url):
            raise RuntimeError(f"{url} is on a site we never fetch")
        if number > 0:
            time.sleep(PAUSE_SECONDS)
        page = render(url)
        if page["blocked"]:
            # Never try to get around bot protection
            raise RuntimeError(f"{url} blocked by bot protection (not bypassed)")
        page_jobs = jobs_from_links(page["links"], url, page["final_url"])
        if text_list:
            page_jobs += jobs_from_text_list(
                page["text"], page["final_url"], text_list["start"], text_list["end"]
            )
        if not page_jobs:
            page_jobs = jobs_from_text(page["text"], page["final_url"])
        jobs.extend(page_jobs)
    return jobs


# ---------------------------------------------------------------------------
# One company
# ---------------------------------------------------------------------------
def fetch_company(company, settings):
    """Return (jobs, source, note) for one company."""
    ats = company["ats"].strip()
    board = company["ats_board_id"].strip()
    careers_url = company["careers_url"].strip()

    if ats in FEED_ATS and board:
        try:
            return FEEDS[ats](board, careers_url), "feed", ""
        except (requests.RequestException, ValueError) as error:
            if not careers_url:
                raise
            time.sleep(PAUSE_SECONDS)
            note = f"feed failed ({error}); rendered careers page instead"
            return render_jobs(careers_url, settings), "render", note

    if not careers_url:
        raise RuntimeError("no careers_url to render")
    note = ""
    if ats == "comeet":
        note = "Comeet feed needs a private token; rendered careers page"
    elif ats in FEED_ATS:
        note = "no board ID; rendered careers page"
    return render_jobs(careers_url, settings), "render", note


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Make printing Hebrew safe on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Fetch open jobs for companies.csv")
    parser.add_argument("--companies", help='Only these companies, e.g. "Immunai,Protai"')
    args = parser.parse_args()

    with open(COMPANIES_CSV, encoding="utf-8", newline="") as f:
        companies = list(csv.DictReader(f))

    # Per-company settings (extra pages, text lists, filters)
    all_settings = {}
    if SETTINGS_FILE.exists():
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            all_settings = json.load(f)

    # Companies with a job_filter take their jobs from a portal shared with
    # another company. Handle them first, so the portal's owner (e.g. Clalit)
    # gets only the jobs left over, whatever the row order in companies.csv.
    # (sort keeps the original order within each group)
    def has_filter(company):
        return bool(all_settings.get(company["name"].strip(), {}).get("job_filter"))
    companies.sort(key=lambda c: 0 if has_filter(c) else 1)

    if args.companies:
        wanted = [n.strip().lower() for n in args.companies.split(",") if n.strip()]
        companies = [c for c in companies if c["name"].strip().lower() in wanted]
        found = {c["name"].strip().lower() for c in companies}
        for name in wanted:
            if name not in found:
                print(f"Warning: '{name}' is not in companies.csv")

    all_jobs = []
    summary = []  # one line per company
    first_request = True
    seen_jobs = set()  # (link, title) of every job saved so far
    total_duplicates = 0

    for company in companies:
        name = company["name"].strip()
        ats = company["ats"].strip()

        # Companies marked "manual" in company_settings.json are checked by hand
        manual_reason = all_settings.get(name, {}).get("manual")
        if manual_reason:
            summary.append((name, ats, "skipped", 0, 0, f"check manually: {manual_reason}", []))
            continue
        if ats in ("none", "blocked"):
            summary.append((name, ats, "skipped", 0, 0, f"ats is {ats}", []))
            continue
        if ats not in FEED_ATS and ats not in RENDER_ATS:
            summary.append((name, ats, "skipped", 0, 0, f"no method for ats '{ats}'", []))
            continue

        if not first_request:
            time.sleep(PAUSE_SECONDS)
        first_request = False

        settings = all_settings.get(name, {})
        print(f"\n--- {name} ({ats}) ---")
        try:
            jobs, source, note = fetch_company(company, settings)
        except Exception as error:  # report the problem and move on
            summary.append((name, ats, "error", 0, 0, str(error)[:200], []))
            continue

        # Shared portals: keep only the jobs that name this company
        filter_words = settings.get("job_filter")
        if filter_words:
            before = len(jobs)
            jobs = [
                j for j in jobs
                if any(w in j.get("context", "") + " " + j["title"] + " "
                       + j["description"] + " " + j["location"] for w in filter_words)
            ]
            note = (note + "; " if note else "") + \
                f"filter kept {len(jobs)} of {before} portal jobs"

        fetched_at = datetime.now().isoformat(timespec="seconds")
        israel_only = company["type"].strip() in ISRAEL_ONLY_TYPES
        kept = []
        duplicates = 0
        for job in jobs:
            # Skip a job already saved (for this or an earlier company)
            key = (job["url"], job["title"])
            if key in seen_jobs:
                duplicates += 1
                continue
            seen_jobs.add(key)

            location = job["location"]
            # Show "Remote" in the location when the feed says so
            if job["remote"] and not contains_word(location.lower(), REMOTE_WORDS):
                location = f"{location} (Remote)" if location else "Remote"
            if israel_only:
                status = "israel"
                location = location or "Israel"
            else:
                status = classify_location(location, job["title"], job["url"])
            if status == "foreign":
                continue
            kept.append({
                "company": name,
                "title": job["title"],
                "location": location,
                "location_status": status,  # israel / unclear
                "url": job["url"],
                "source": source,
                "fetched_at": fetched_at,
                "description": job["description"],
            })
        all_jobs.extend(kept)
        if duplicates:
            total_duplicates += duplicates
            note = (note + "; " if note else "") + f"{duplicates} duplicates skipped"
        summary.append((name, ats, source, len(jobs), len(kept), note, kept))

    # Save the jobs
    JOBS_DIR.mkdir(exist_ok=True)
    out_path = JOBS_DIR / f"{date.today().isoformat()}_raw.json"
    # Keep jobs that the read-alerts skill already added from alert emails
    # (source "email"); only the fetched jobs are replaced
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            email_jobs = [j for j in json.load(f) if j.get("source") == "email"]
        if email_jobs:
            print(f"Keeping {len(email_jobs)} jobs from alert emails already in {out_path}")
            all_jobs.extend(email_jobs)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_jobs, f, ensure_ascii=False, indent=2)

    # Save the per-company summary too (the digest lists fetch errors)
    log_path = JOBS_DIR / f"{date.today().isoformat()}_fetch_log.json"
    log = [
        {"company": name, "ats": ats, "method": source,
         "found": found, "kept": kept_count, "note": note}
        for name, ats, source, found, kept_count, note, _ in summary
    ]
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    # Print the summary
    print("\n=== Summary ===")
    for name, ats, source, found, kept_count, note, kept in summary:
        line = f"{name} [{ats}, {source}]: found {found}, kept {kept_count}"
        if note:
            line += f"  ({note})"
        print(line)
        for job in kept:
            flag = "" if job["location_status"] == "israel" else f"  [{job['location_status']}]"
            print(f"    - {job['title']}{flag}")

    # Totals per method, and the list of errors
    print("\n=== Totals per method ===")
    for method in ("feed", "render", "skipped", "error"):
        rows = [s for s in summary if s[2] == method]
        found = sum(s[3] for s in rows)
        kept_total = sum(s[4] for s in rows)
        print(f"{method}: {len(rows)} companies, found {found}, kept {kept_total}")
    print(f"duplicates skipped: {total_duplicates}")
    errors = [s for s in summary if s[2] == "error"]
    if errors:
        print("\n=== Errors ===")
        for name, ats, _, _, _, note, _ in errors:
            print(f"{name} [{ats}]: {note}")
    print(f"\nSaved {len(all_jobs)} jobs to {out_path}")


if __name__ == "__main__":
    main()
