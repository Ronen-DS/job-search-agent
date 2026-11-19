# Opens a web page in a headless (invisible) Chromium browser, lets its
# JavaScript run, and saves what a person would see: the page text, its links,
# and any embedded frames/scripts. It also lists hiring-system (ATS) signatures.
#
# Usage:   python scripts/render_page.py <url>
# Output:  renders/<name-made-from-url>.txt  (the path is printed at the end)

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Text fragments that reveal which hiring system powers a careers page.
# Each system can have several fragments; finding any one of them counts.
ATS_SIGNATURES = {
    "comeet": ["comeet"],
    "greenhouse": ["greenhouse.io"],
    "lever": ["lever.co"],
    "ashby": ["ashbyhq"],
    "workable": ["workable.com"],
    "workday": ["myworkdayjobs"],
    "smartrecruiters": ["smartrecruiters"],
    "recruitee": ["recruitee.com"],
    "eightfold": ["eightfold.ai"],
    "redmatch": ["redmatch", "topmatch"],  # Israeli system, e.g. hospital /Positions portals
}

OUTPUT_DIR = Path("renders")


def url_to_filename(url):
    """Turn a URL into a safe file name, e.g. https://cgen.com/careers -> cgen.com_careers.txt"""
    name = re.sub(r"^https?://", "", url)
    name = re.sub(r"[^A-Za-z0-9.-]+", "_", name).strip("_")
    return name[:120] + ".txt"


def render(url):
    """Render the page, save the text file, print a short summary and
    return the collected data as a dictionary (used by fetch_jobs.py)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=45000)
        # Wait until the page stops loading data; some sites never fully settle,
        # so after 15 seconds we just continue with what has loaded.
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeout:
            pass
        # Workday career sites draw their job list late, so wait (up to 20
        # seconds) until the first job link appears.
        if "myworkdayjobs" in url:
            try:
                page.wait_for_selector("a[href*='/job/']", timeout=20000)
            except PlaywrightTimeout:
                pass

        title = page.title()
        final_url = page.url  # may differ from url if the site redirected
        text = page.inner_text("body")
        # Collect every link as a pair: [link text, address]
        link_pairs = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => [e.innerText.trim(), e.href])"
        )
        links = [f"{text} -> {href}" for text, href in link_pairs]
        # Embedded frames and scripts often point to the ATS (e.g. a Comeet widget)
        frames = [f.url for f in page.frames if f.url and f.url != "about:blank"]
        scripts = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
        html = page.content()
        browser.close()

    # Look for ATS signatures anywhere in the page source, links, frames or scripts
    everything = (html + "\n".join(links + frames + scripts)).lower()
    found_ats = [
        name for name, sigs in ATS_SIGNATURES.items()
        if any(sig in everything for sig in sigs)
    ]

    # Detect bot-protection pages (e.g. Cloudflare "Just a moment...").
    # We do not try to get around these; we just report them.
    # We look at the visible title and text only, not at scripts: many normal
    # pages load a Cloudflare widget for their forms without blocking anyone.
    block_titles = ["just a moment...", "attention required! | cloudflare", "radware captcha page"]
    block_phrases = [
        "performing security verification",        # Cloudflare check
        "sorry, you have been blocked",             # Cloudflare block
        "made us think that you are a bot",         # Radware captcha
    ]
    visible = (title + "\n" + text).lower()
    blocked = title.strip().lower() in block_titles or any(p in visible for p in block_phrases)
    if blocked:
        print("BLOCKED: the site showed a bot-protection check; the real page was not read.")

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / url_to_filename(url)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"URL: {url}\nFINAL URL: {final_url}\nTITLE: {title}\n")
        f.write(f"ATS SIGNATURES: {', '.join(found_ats) or 'none found'}\n")
        f.write(f"BLOCKED BY BOT PROTECTION: {'yes' if blocked else 'no'}\n\n")
        f.write("== FRAMES ==\n" + "\n".join(frames) + "\n\n")
        f.write("== SCRIPTS ==\n" + "\n".join(scripts) + "\n\n")
        f.write("== LINKS ==\n" + "\n".join(links) + "\n\n")
        f.write("== TEXT ==\n" + text + "\n")

    print(f"ATS signatures: {', '.join(found_ats) or 'none found'}")
    print(f"Saved to {out_path}")

    return {
        "url": url,
        "final_url": final_url,
        "title": title,
        "text": text,
        "links": [{"text": t, "href": h} for t, h in link_pairs],
        "found_ats": found_ats,
        "blocked": blocked,
        "out_path": str(out_path),
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/render_page.py <url>")
        sys.exit(1)
    render(sys.argv[1])
