# Helper script for the read-alerts skill (job-alert emails -> raw jobs).
#
# The emails themselves are read by Claude through the Gmail connector
# (search and read tools only). This script does everything around that:
#
#   start   : prints the trusted senders (read from CLAUDE.md), the Gmail
#             search window and how many emails were already processed.
#             Records the run's start time in state/email_run.json.
#   show    : turns one saved Gmail message (the JSON file the connector
#             result was saved to) into short plain text, with every link
#             cleaned (see clean_url), so the jobs can be read off it.
#   commit  : the last step, run only when every email was read. Reads
#             jobs/<date>_email_jobs.json (written by Claude), checks it,
#             appends the jobs to jobs/<date>_raw.json with source "email",
#             adds new companies to companies.csv, and only then updates
#             state/processed_emails.csv and state/email_state.json.
#
# Usage (from the project root):
#   python .claude/skills/read-alerts/read_alerts.py start
#   python .claude/skills/read-alerts/read_alerts.py show <saved_message_file>
#   python .claude/skills/read-alerts/read_alerts.py commit [YYYY-MM-DD]

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Reuse the location rules and paths from the fetch-jobs skill
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / ".claude" / "skills" / "fetch-jobs"))
import fetch_jobs as fj  # noqa: E402  (import after the path change)

JOBS_DIR = fj.JOBS_DIR
STATE_DIR = PROJECT_ROOT / "state"
STATE_FILE = STATE_DIR / "email_state.json"        # {"last_successful_run": ...}
PROCESSED_FILE = STATE_DIR / "processed_emails.csv"
RUN_FILE = STATE_DIR / "email_run.json"            # start time of the current run
CLAUDE_MD = PROJECT_ROOT / "CLAUDE.md"
COMPANIES_CSV = PROJECT_ROOT / "companies.csv"
REJECTED_CSV = PROJECT_ROOT / "rejected_companies.csv"

FIRST_RUN_DAYS = 14   # look-back on the very first run
OVERLAP_DAYS = 1      # search from the last successful run minus this

# Query parameters that identify the mailbox owner or log them in.
# They are removed from every link (lower case; utm_* is removed too).
PRIVATE_PARAMS = {
    "userid", "uid", "key", "token", "otptoken", "midtoken", "midsig",
    "sig", "signature", "email", "e", "auth", "session", "sessionid",
    "packagejobskey", "trackingid", "refid", "lipi", "trk", "trkemail", "eid",
}
# Words that must never survive in a stored link
FORBIDDEN_IN_URL = ["userid=", "key=", "token=", "otptoken", "mailsredirect"]


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------
def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_csv(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def processed_ids():
    return {r["email_id"] for r in read_csv(PROCESSED_FILE)}


# ---------------------------------------------------------------------------
# Trusted senders (read from CLAUDE.md every run, never copied here)
# ---------------------------------------------------------------------------
def trusted_senders():
    """The list after 'trusted alert senders:' in CLAUDE.md, e.g.
    ['AllJobs', 'JobMaster', 'Jobify', 'jobalerts-noreply@linkedin.com']."""
    text = CLAUDE_MD.read_text(encoding="utf-8")
    match = re.search(r"trusted alert senders:\s*([^\n]+)", text, re.I)
    if not match:
        sys.exit("Could not find 'trusted alert senders:' in CLAUDE.md. Stopping.")
    line = match.group(1).split(". ")[0]          # stop at the end of the sentence
    line = re.sub(r"\([^)]*\)", "", line)         # drop notes like "(forwarded)"
    parts = re.split(r",|\band\b", line)
    senders = [p.strip().strip(".") for p in parts if p.strip().strip(".")]
    if not senders:
        sys.exit("The trusted sender list in CLAUDE.md is empty. Stopping.")
    return senders


def registrable_label(domain):
    """'alljob' for 'mail.alljob.co.il', 'linkedin' for 'linkedin.com'."""
    labels = domain.lower().split(".")
    # Israeli and UK style suffixes have two parts (co.il, org.il, co.uk)
    two_part = len(labels) >= 3 and labels[-2] in {"co", "org", "net", "ac", "gov"}
    return labels[-3] if two_part else labels[-2] if len(labels) >= 2 else ""


def is_trusted(sender, senders):
    """sender is an email address. An entry with '@' must match exactly.
    A name (e.g. AllJobs) must be the sender's own domain name, so
    alljob.co.il matches AllJobs but alljob.example.com does not.
    The display name is ignored: anyone can type any display name."""
    sender = sender.strip().lower()
    if "@" not in sender:
        return False
    domain = sender.split("@", 1)[1]
    label = registrable_label(domain)
    for entry in senders:
        entry = entry.lower()
        if "@" in entry:
            if sender == entry:
                return True
        else:
            name = re.sub(r"[^a-z0-9]", "", entry)
            if label and label in (name, name.rstrip("s")):
                return True
    return False


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------
def is_linkedin(url):
    host = urlparse(url).netloc.lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def clean_url(url):
    """A link that is safe to store and to open:
    - AllJobs alert links are personal redirects (user ID + login key) to
      the 'send CV' page. Only the JobID is kept, giving the public job page.
    - LinkedIn links are never stored or opened: returns ''.
    - Other links lose every parameter that identifies the mailbox owner."""
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return ""
    if is_linkedin(url):
        return ""
    parts = urlparse(url)
    if "alljobs.co.il" in parts.netloc.lower():
        job_id = re.search(r"JobID=(\d+)", url, re.I)
        if not job_id:
            return ""  # menus, unsubscribe and other non-job links
        return f"https://www.alljobs.co.il/Search/UploadSingle.aspx?JobID={job_id.group(1)}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in PRIVATE_PARAMS and not k.lower().startswith("utm_")]
    return urlunparse(parts._replace(query=urlencode(query), fragment=""))


def url_is_safe(url):
    low = url.lower()
    return not is_linkedin(url) and not any(w in low for w in FORBIDDEN_IN_URL)


# ---------------------------------------------------------------------------
# show: a saved Gmail message -> short text
# ---------------------------------------------------------------------------
class TextAndLinks(HTMLParser):
    """Collects the visible text of an HTML email. Each link becomes
    [link: <cleaned url>] (LinkedIn links become [linkedin link])."""
    BLOCK_TAGS = {"br", "p", "div", "tr", "td", "li", "table", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0          # inside <style> or <script>
        self.last_link = None

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self.skip += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            label = "[linkedin link]" if is_linkedin(href) else clean_url(href)
            if label and label != self.last_link:
                self.parts.append(f" [link: {label}] ")
                self.last_link = label

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def message_text(msg):
    """Plain text of a Gmail message with cleaned links."""
    plain = msg.get("plaintextBody") or ""
    if plain.strip():
        def replace(match):
            url = match.group(0)
            return "[linkedin link]" if is_linkedin(url) else f"[link: {clean_url(url) or 'removed'}]"
        text = re.sub(r"https?://\S+", replace, plain)
    else:
        parser = TextAndLinks()
        parser.feed(msg.get("htmlBody") or "")
        text = "".join(parser.parts)
    # Tidy up: trim lines, drop empty ones
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def step_show(path_text):
    msg = load_json(Path(path_text))
    if msg is None:
        sys.exit(f"File not found: {path_text}")
    senders = trusted_senders()
    sender = msg.get("sender", "")
    print(f"id: {msg.get('id')}\nsender: {sender}\ndate: {msg.get('date')}\n"
          f"subject: {msg.get('subject')}")
    if not is_trusted(sender, senders):
        print("NOT a trusted sender: ignore this email.")
        return
    if msg.get("id") in processed_ids():
        print("Already processed in an earlier run: skip.")
        return
    print("---")
    print(message_text(msg))


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------
def step_start():
    senders = trusted_senders()
    state = load_json(STATE_FILE, {})
    now = datetime.now().astimezone()
    if state.get("last_successful_run"):
        last = datetime.fromisoformat(state["last_successful_run"])
        since = last - timedelta(days=OVERLAP_DAYS)
        why = f"last successful run {last:%Y-%m-%d %H:%M} minus {OVERLAP_DAYS} day"
    else:
        since = now - timedelta(days=FIRST_RUN_DAYS)
        why = f"first run: {FIRST_RUN_DAYS} days back"
    save_json(RUN_FILE, {"started": now.isoformat(timespec="seconds"),
                         "search_after": since.strftime("%Y/%m/%d")})
    print("Trusted senders (from CLAUDE.md): " + ", ".join(senders))
    print(f"Search window: {why}")
    # Gmail's after: is a whole day, from midnight
    print(f"Gmail query: after:{since:%Y/%m/%d}")
    print(f"Emails already processed: {len(processed_ids())} (skip these IDs)")
    print(f"Write the extracted jobs to jobs/{date.today().isoformat()}_email_jobs.json, "
          "then run commit.")


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------
def name_key(name):
    """Company name for comparing: lower case, no Ltd/Inc/בע"מ, no punctuation."""
    name = name.lower()
    name = re.sub(r"\b(ltd|inc|llc|gmbh|corp|israel)\b\.?", " ", name)
    name = name.replace('בע"מ', " ").replace("בעמ", " ")
    return re.sub(r"[^\w]+", " ", name).strip()


def title_key(title):
    return re.sub(r"[^\w]+", " ", title.lower()).strip()


def append_companies(rows):
    """Append rows to companies.csv, keeping its columns."""
    with open(COMPANIES_CSV, encoding="utf-8", newline="") as f:
        columns = next(csv.reader(f))
    with open(COMPANIES_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def step_commit(day):
    run = load_json(RUN_FILE)
    if not run:
        sys.exit("No run in progress: run 'start' first.")
    in_path = JOBS_DIR / f"{day}_email_jobs.json"
    data = load_json(in_path)
    if data is None:
        sys.exit(f"Missing {in_path}")

    senders = trusted_senders()
    done = processed_ids()
    problems = []

    # --- Emails: trusted, not processed before -----------------------------
    emails = {}
    for e in data.get("emails", []):
        if not is_trusted(e.get("sender", ""), senders):
            problems.append(f"email {e.get('id')} is from an untrusted sender {e.get('sender')}")
        elif e["id"] in done:
            print(f"Skipping email {e['id']}: already processed")
        else:
            emails[e["id"]] = e

    # --- Jobs -----------------------------------------------------------------
    raw_path = JOBS_DIR / f"{day}_raw.json"
    raw = load_json(raw_path, [])
    # Duplicates: the same link, or the same company + title as a job
    # fetched from a careers page (or as an earlier LinkedIn job, which has
    # no link). Two alert jobs with different links are different jobs,
    # even with the same title.
    seen_urls = {fj.normalize_url(j["url"]) for j in raw if j["url"]}
    seen_titles = {(name_key(j["company"]), title_key(j["title"])) for j in raw
                   if j.get("source") != "email" or j.get("linkedin_only")}

    fetched_at = datetime.now().isoformat(timespec="seconds")
    new_jobs, stats = [], defaultdict(Counter)
    for job in data.get("jobs", []):
        email = emails.get(job.get("email_id"))
        if email is None:
            continue  # its email was skipped or is untrusted
        who = email["sender"].lower()
        stats[who]["extracted"] += 1
        title = (job.get("title") or "").strip()
        company = (job.get("company") or "").strip()
        if not title or not company:
            problems.append(f"job without company or title in email {email['id']}: {job}")
            continue
        original_url = job.get("url") or ""
        linkedin = bool(job.get("linkedin")) or is_linkedin(original_url) or \
            "linkedin.com" in who
        url = "" if linkedin else clean_url(original_url)
        if url and not url_is_safe(url):
            problems.append(f"unsafe link left in job '{title}': {url}")
            continue
        if not url and not linkedin:
            problems.append(f"job '{title}' ({company}) has no usable link")
            continue

        location = (job.get("location") or "").strip()
        status = fj.classify_location(location, title, url)
        # Israeli job boards (AllJobs, JobMaster...) list Israeli jobs, often
        # with only a region ("מרכז", "מספר אזורים")
        if status == "unclear" and who.endswith(".co.il"):
            status = "israel"
        if status == "foreign":
            stats[who]["abroad (dropped)"] += 1
            continue
        # Already in today's raw file (from fetch-jobs or an earlier alert)
        if (url and fj.normalize_url(url) in seen_urls) or \
                (name_key(company), title_key(title)) in seen_titles:
            stats[who]["duplicates"] += 1
            continue
        if url:
            seen_urls.add(fj.normalize_url(url))
        if linkedin:
            seen_titles.add((name_key(company), title_key(title)))

        new_jobs.append({
            "company": company,
            "title": title,
            "location": location,
            "location_status": status,
            "url": url,
            "source": "email",
            "fetched_at": fetched_at,
            "description": "",
            "alert_sender": email["sender"],
            "email_id": email["id"],
            "linkedin_only": linkedin,
        })
        stats[who]["kept"] += 1
        if linkedin:
            stats[who]["LinkedIn (company + title only)"] += 1

    # --- Companies --------------------------------------------------------------
    known = {name_key(c["name"]) for c in read_csv(COMPANIES_CSV)}
    rejected = {name_key(c["name"]) for c in read_csv(REJECTED_CSV)}
    decisions = {name_key(c["name"]): c for c in data.get("new_companies", [])}
    to_add, not_added = [], []
    for name in sorted({j["company"] for j in new_jobs}):
        key = name_key(name)
        if key in known:
            continue
        if key in rejected:
            not_added.append((name, "in rejected_companies.csv"))
            continue
        decision = decisions.get(key)
        if decision is None:
            problems.append(f"no add/skip decision for new company '{name}' in new_companies")
            continue
        if not decision.get("add"):
            not_added.append((name, decision.get("reason", "")))
            continue
        first_job = next(j for j in new_jobs if name_key(j["company"]) == key)
        sender_name = first_job["alert_sender"].split("@")[1]
        to_add.append({
            "name": name,
            "type": decision.get("type", "company"),
            "category": decision.get("category", ""),
            "website": decision.get("website", ""),
            "source": "email alert",
            "date_added": day,
            "source_link": first_job["url"],  # the public job link ('' for LinkedIn)
            "reason": decision.get("reason") or
                      f"job alert from {sender_name}: {first_job['title']}",
        })
        known.add(key)
        stats[first_job["alert_sender"].lower()]["new companies"] += 1

    # --- Stop before changing anything if there is a problem ------------------------
    if problems:
        print("NOT committed (nothing was changed). Fix these first:")
        for p in problems:
            print("  - " + p)
        sys.exit(1)

    # --- Save: raw jobs, companies, then the state (last) --------------------
    save_json(raw_path, raw + new_jobs)
    if to_add:
        append_companies(to_add)
    STATE_DIR.mkdir(exist_ok=True)
    new_file = not PROCESSED_FILE.exists()
    with open(PROCESSED_FILE, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["email_id", "sender", "received", "processed_on", "jobs"])
        per_email = Counter(j.get("email_id") for j in data.get("jobs", []))
        for e in emails.values():
            writer.writerow([e["id"], e["sender"], e.get("date", ""), day, per_email[e["id"]]])
    save_json(STATE_FILE, {"last_successful_run": run["started"]})
    RUN_FILE.unlink()

    # --- Summary per sender -----------------------------------------------------
    print("Per sender:")
    per_sender_emails = Counter(e["sender"].lower() for e in emails.values())
    for who in sorted(per_sender_emails):
        counts = stats[who]
        extra = ", ".join(f"{k}: {v}" for k, v in counts.items()
                          if k not in ("extracted", "kept", "new companies"))
        print(f"  {who}: emails read {per_sender_emails[who]}, jobs extracted "
              f"{counts['extracted']}, kept {counts['kept']}, new companies "
              f"{counts['new companies']}" + (f" ({extra})" if extra else ""))
    for c in to_add:
        print(f"  + added company: {c['name']} ({c['category']})")
    for name, reason in not_added:
        print(f"  - not added: {name}: {reason}")
    print(f"Appended {len(new_jobs)} jobs to {raw_path}")
    print(f"State saved: {len(emails)} emails marked processed; "
          f"last successful run = {run['started']}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="read-alerts helper: start, show, commit")
    parser.add_argument("step", choices=["start", "show", "commit"])
    parser.add_argument("arg", nargs="?", help="show: saved message file; commit: date")
    args = parser.parse_args()
    if args.step == "start":
        step_start()
    elif args.step == "show":
        if not args.arg:
            sys.exit("show needs the path of a saved message file")
        step_show(args.arg)
    else:
        step_commit(args.arg or date.today().isoformat())


if __name__ == "__main__":
    main()
