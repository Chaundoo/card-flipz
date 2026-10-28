"""Admin log for the site (botlog.json): one plain-English entry per bot run.

Scripts call summary("...headline...", facts=[...]) to leave a one-line result in run_summary.json.
Every workflow's last step runs scripts/log_run.sh, which calls `python3 scripts/botlog.py record` to
turn that (plus run_report.md and the job's status) into an entry, then commits botlog.json.
The site's Admin tab reads botlog.json and the GitHub Actions run list.
"""
import datetime, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGFILE = os.path.join(ROOT, "botlog.json")
SUMMARY = os.path.join(ROOT, "run_summary.json")
REPORT = os.path.join(ROOT, "run_report.md")
KEEP_DAYS = 35
KEEP_MAX = 400

BOTS = {
    "price": "Daily price update",
    "finder": "Flip Finder",
    "graded": "Graded Slabs",
    "cardprice": "CardPrice lookups",
    "ta": "tcgapi bot (removed)",
    "pa": "pokemon-api.com lookups",
    "tcgcheck": "TCGplayer sales check",
    "addcard": "Add card from link",
    "approve": "Add Flip Finder cards",
    "save": "Save pairings & profit log",
    "outliers": "Fix outlier prices",
    "patch": "One-time patch",
}
OK = {"ok", "added", "saved", "duplicate", "nothing", "success"}
WARN = {"partial"}
FAIL = {"failed", "failure", "blocked", "error", "cancelled"}


def summary(headline, level=None, facts=None):
    """Called by the bot scripts: the one-line result shown in the admin log."""
    try:
        with open(SUMMARY, "w", encoding="utf-8") as f:
            json.dump({"headline": headline, "level": level, "facts": facts or []}, f, ensure_ascii=False)
    except OSError:
        pass


def plain(line):
    line = re.sub(r"^#+\s*", "", line.strip())
    line = re.sub(r"\*\*|`|<[^>]+>", "", line)
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
    return re.sub(r"^[^\w$]+", "", line).strip()


def record():
    bot = os.environ.get("BOT", "other")
    job = (os.environ.get("JOB_STATUS") or "").lower()
    status = (os.environ.get("RUN_STATUS") or "").lower()
    dry = os.environ.get("DRY", "false") == "true"
    report = open(REPORT, encoding="utf-8").read() if os.path.exists(REPORT) else ""
    report = "\n".join(l for l in report.splitlines() if not l.startswith("[Run details]")).strip()
    summ = {}
    if os.path.exists(SUMMARY):
        try:
            summ = json.load(open(SUMMARY, encoding="utf-8"))
        except ValueError:
            summ = {}
    if status == "skipped" or (not report and not summ and job == "success"):
        print("Nothing to log (a wake-up that didn't run, or no report).")
        return 0

    level = summ.get("level")
    if not level:
        if job in FAIL or status in FAIL:
            level = "fail"
        elif status in WARN:
            level = "warn"
        else:
            level = "ok"
    if job in FAIL and level == "ok":
        level = "warn"   # the bot finished but a later step (saving, posting) failed

    headline = summ.get("headline")
    if not headline:
        first = next((plain(l) for l in report.splitlines() if plain(l)), "")
        if first:
            headline = first
        elif level == "fail":
            headline = f"{BOTS.get(bot, bot)} crashed before it could say what happened. Open the run for the error."
        else:
            headline = f"{BOTS.get(bot, bot)} finished."
    if job in FAIL and level != "fail" and "saving" not in headline:
        headline += " (but a later step of the run failed, e.g. saving: check the run)"
    if job == "cancelled":
        headline = f"{BOTS.get(bot, bot)} was cancelled or timed out. " + headline

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    entry = {
        "t": now.isoformat().replace("+00:00", "Z"), "bot": bot, "name": BOTS.get(bot, bot),
        "level": level, "headline": headline, "facts": summ.get("facts") or [],
        "event": os.environ.get("EVENT", ""), "dry": dry, "run": os.environ.get("RUN_URL", ""),
        "details": report[:6000] + ("\n…(cut short; see the run)" if len(report) > 6000 else ""),
    }
    data = {"entries": []}
    if os.path.exists(LOGFILE):
        try:
            data = json.load(open(LOGFILE, encoding="utf-8"))
        except ValueError:
            pass
    cut = (now - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    entries = [e for e in data.get("entries", []) if (e.get("t") or "") >= cut[:19]]
    entries = ([entry] + entries)[:KEEP_MAX]
    data = {"about": "Written by scripts/botlog.py after every bot run. Read by the site's Admin tab.", "entries": entries}
    with open(LOGFILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Logged: [{level}] {headline}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "record":
        sys.exit(record())
    print(__doc__)
