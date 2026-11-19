# Checks a weekly digest: every job in it must have a link that read posting
# confirmed live in this run (jobs/<date>_read_posting.json, status "live").
#
# Job sections checked: "New strong matches", "New possible matches",
# "Still open from earlier weeks" and "Long-open". "Closed since last week"
# lists jobs that are gone, and the other sections list companies, so those
# are not checked.
#
# Two ways to use it:
#   1. As a Claude Code PreToolUse hook (Write / Edit / MultiEdit): reads the
#      tool call as JSON on stdin. If the file is digests/<date>.md and the
#      new content fails the check, it exits with code 2, which blocks the
#      write and shows the reason to Claude.
#   2. From the command line or another script:
#        python .claude/hooks/check_digest.py digests/2026-11-18.md
#      Exit code 0 = OK, 1 = problems found (printed).

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JOBS_DIR = PROJECT_ROOT / "jobs"
JOB_SECTIONS = {
    "New strong matches",
    "New possible matches",
    "Still open from earlier weeks",
    "Long-open",
}
DIGEST_NAME = re.compile(r"(?:^|[\\/])digests[\\/](\d{4}-\d{2}-\d{2})\.md$")
URL = re.compile(r"https?://[^\s)>\]]+")


def live_urls(day):
    """Links that read posting confirmed live in the run for this date."""
    path = JOBS_DIR / f"{day}_read_posting.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return {j["url"] for j in json.load(f) if j.get("status") == "live"}


def job_items(text):
    """Yield (section, item_text) for every job in the job sections.
    A job is a '### ' block, or a '- **' bullet (used in Long-open)."""
    section = None
    item = None
    for line in text.splitlines():
        if line.startswith("## "):
            if item:
                yield section, item
            section, item = line[3:].strip(), None
            continue
        if section not in JOB_SECTIONS:
            continue
        if line.startswith("### ") or line.startswith("- **"):
            if item:
                yield section, item
            item = line
        elif item is not None:
            item += "\n" + line
    if item:
        yield section, item


def validate(text, day):
    """Return a list of problems (empty list = the digest is OK)."""
    items = list(job_items(text))
    if not items:
        return []  # no jobs in the job sections: nothing to confirm
    live = live_urls(day)
    if live is None:
        return [f"no read-posting file jobs/{day}_read_posting.json for this run"]
    problems = []
    for section, item in items:
        name = item.splitlines()[0][:80]
        urls = [u.rstrip(".,;") for u in URL.findall(item)]
        if not urls:
            problems.append(f"[{section}] {name}: no link")
        elif not any(u in live for u in urls):
            problems.append(f"[{section}] {name}: link not confirmed by read posting "
                            f"in this run ({urls[0]})")
    return problems


def new_content(tool_name, tool_input, path):
    """The file content after the Write / Edit / MultiEdit would run."""
    if tool_name == "Write":
        return tool_input.get("content", "")
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    edits = tool_input.get("edits") if tool_name == "MultiEdit" else [tool_input]
    for edit in edits or []:
        old, new = edit.get("old_string", ""), edit.get("new_string", "")
        if edit.get("replace_all"):
            current = current.replace(old, new)
        else:
            current = current.replace(old, new, 1)
    return current


def run_as_hook():
    data = json.load(sys.stdin)
    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path", "")
    match = DIGEST_NAME.search(file_path)
    if not match:
        sys.exit(0)  # not a digest: nothing to check
    text = new_content(data.get("tool_name", ""), tool_input, Path(file_path))
    problems = validate(text, match.group(1))
    if problems:
        print("Digest rejected: every job must have a link confirmed live by read posting "
              "in this run.",
              file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        sys.exit(2)  # exit code 2 blocks the tool call
    sys.exit(0)


def run_from_command_line(path_text):
    path = Path(path_text)
    match = DIGEST_NAME.search(str(path))
    if not match:
        print("Expected a path like digests/YYYY-MM-DD.md")
        sys.exit(1)
    problems = validate(path.read_text(encoding="utf-8"), match.group(1))
    for p in problems:
        print(p)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1:
        run_from_command_line(sys.argv[1])
    else:
        run_as_hook()
