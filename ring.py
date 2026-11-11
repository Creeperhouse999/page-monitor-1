"""
Failover ring for the monitor: page-monitor-1 .. page-monitor-10 (same code in every repo).

Only ONE repo watches at a time (the "leader" = a repo with a running `watch` job).
Every repo runs a sentinel that checks the whole ring every 2 min; if nobody is watching,
the next repo in line takes over (staggered delays so they don't all start at once).

  python ring.py leaders            -> print repos with a live watch job
  python ring.py should-run         -> exit 1 if another repo is already watching (so this one stands down)
  python ring.py sync               -> merge known_links.json / status.json from all ring repos
  python ring.py sentinel MINUTES   -> watch the ring for MINUTES, take over if nobody watches

Env: GITHUB_TOKEN, GITHUB_REPOSITORY (owner/name), NTFY_TOPIC, optional RING_SIZE (10), NTFY_SERVER.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OWNER, SELF = (os.environ.get("GITHUB_REPOSITORY") or "Creeperhouse999/page-monitor-1").split("/")
PREFIX = os.environ.get("RING_PREFIX", "page-monitor-")
SIZE = int(os.environ.get("RING_SIZE", "10"))
RING = [f"{PREFIX}{i}" for i in range(1, SIZE + 1)]
MY_INDEX = RING.index(SELF) if SELF in RING else 0
TOKEN = os.environ.get("GITHUB_TOKEN", "")
NTFY = f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')}/{os.environ.get('NTFY_TOPIC', '')}"
JOB_MAX_AGE = 80 * 60  # a real watch job never lasts longer (timeout 75 min) -> older = ghost


def log(msg):
    print(f"{datetime.now(timezone.utc):%H:%M:%S} [ring {SELF}] {msg}", flush=True)


def api(path):
    req = urllib.request.Request(f"https://api.github.com/{path}",
                                 headers={"Accept": "application/vnd.github+json",
                                          **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def ntfy(msg, title, tags="warning", priority="high"):
    if not NTFY.rsplit("/", 1)[1]:
        return
    url = NTFY + "?" + urllib.parse.urlencode({"title": title, "tags": tags, "priority": priority})
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=msg.encode(), method="POST"), timeout=30).read()
    except Exception as e:
        log(f"ntfy failed: {e}")


def live_watch(repo):
    """True if `repo` has a real (non-test) watch job running right now, started < 80 min ago."""
    try:
        runs = api(f"repos/{OWNER}/{repo}/actions/workflows/watch.yml/runs?per_page=10")["workflow_runs"]
    except Exception as e:
        log(f"{repo}: can't read runs ({e.__class__.__name__})")
        return False
    nowt = time.time()
    for run in runs:
        if run.get("display_title") != "watch" or run["status"] not in ("in_progress", "queued"):
            continue
        try:
            jobs = api(f"repos/{OWNER}/{repo}/actions/runs/{run['id']}/jobs")["jobs"]
        except Exception:
            continue
        for j in jobs:
            if j["status"] == "in_progress" and j.get("started_at"):
                started = datetime.fromisoformat(j["started_at"].replace("Z", "+00:00")).timestamp()
                if nowt - started < JOB_MAX_AGE:
                    return True
        # just dispatched and waiting for a runner counts too (for a few minutes)
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")).timestamp()
        if run["status"] == "queued" and nowt - created < 4 * 60:
            return True
    return False


def leaders():
    return [r for r in RING if live_watch(r)]


def should_run():
    """Stand down if another repo is already watching."""
    others = [r for r in leaders() if r != SELF]
    # two started at once -> the lower number keeps watching, the other stands down
    senior = [r for r in others if RING.index(r) < MY_INDEX] if SELF in RING else others
    if senior or (others and SELF not in RING):
        log(f"already watching: {others} -> this repo stands down")
        return False
    if others:
        log(f"also running: {others} (higher number) -> they will stand down, I keep watching")
    return True


def sync():
    """Merge state from every ring repo: union of known links, newest status."""
    links = set(json.loads(Path("known_links.json").read_text())) if Path("known_links.json").exists() else set()
    status = json.loads(Path("status.json").read_text()) if Path("status.json").exists() else {}
    for repo in RING:
        if repo == SELF:
            continue
        try:
            kl = api(f"repos/{OWNER}/{repo}/contents/known_links.json")
            links |= set(json.loads(base64.b64decode(kl["content"])))
            st = json.loads(base64.b64decode(api(f"repos/{OWNER}/{repo}/contents/status.json")["content"]))
            if st.get("last_scan", 0) > status.get("last_scan", 0):
                status = {**status, **st}
        except Exception:
            pass
    Path("known_links.json").write_text(json.dumps(sorted(links), indent=1))
    if status:
        Path("status.json").write_text(json.dumps(status, indent=1, ensure_ascii=False))
    log(f"synced: {len(links)} known links, pending={status.get('pending')}")


def dispatch_self():
    r = subprocess.run(["gh", "workflow", "run", "watch.yml", "--repo", f"{OWNER}/{SELF}", "--ref", "main"],
                       capture_output=True, text=True)
    log(f"dispatch watch: rc={r.returncode} {r.stderr.strip()[:200]}")
    return r.returncode == 0


def sentinel(minutes):
    """Every 2 min: is anyone watching? If nobody for long enough, this repo takes over.
    Delay grows with distance from the last leader so exactly one repo steps in."""
    end = time.time() + minutes * 60
    nobody_since = first_try = last_try = None
    alarmed = False
    # repo N waits 3 + 2*(N-1) min of "nobody watching" before taking over: 3, 5, 7 ... min
    grace = (3 + 2 * MY_INDEX) * 60
    while time.time() < end:
        live = leaders()
        if live:
            if first_try and SELF in live:
                ntfy(f"Obserwowanie wznowione w {SELF} ✅", "Obserwator działa ✅", tags="white_check_mark",
                     priority="default")
            nobody_since = first_try = last_try = None
            alarmed = False
            log(f"watching: {live}")
        else:
            nobody_since = nobody_since or time.time()
            gap = time.time() - nobody_since
            log(f"NOBODY watching for {gap / 60:.1f} min (my turn after {grace / 60:.0f} min)")
            if gap >= grace:
                if first_try is None:  # alert once per outage
                    ntfy(f"Obserwowanie przerwane od {gap / 60:.0f} min – przejmuje {SELF}.",
                         "⚠️ Obserwator przestał działać")
                if last_try is None or time.time() - last_try > 5 * 60:  # (re)try quietly every 5 min
                    dispatch_self()
                    first_try = first_try or time.time()
                    last_try = time.time()
                if not alarmed and time.time() - first_try > 10 * 60:
                    ntfy(f"{SELF} nie zdołał uruchomić obserwatora od 10 min. "
                         "Kolejne repo z pierścienia przejmie automatycznie.",
                         "🚨 Obserwator nie startuje", tags="rotating_light")
                    alarmed = True
        time.sleep(120)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "leaders"
    if cmd == "leaders":
        print(leaders())
    elif cmd == "should-run":
        sys.exit(0 if should_run() else 1)
    elif cmd == "sync":
        sync()
    elif cmd == "sentinel":
        sentinel(float(sys.argv[2]) if len(sys.argv) > 2 else 45)
    elif cmd == "notify":  # ring.py notify "title" "message" [tags]
        ntfy(sys.argv[3], sys.argv[2], tags=sys.argv[4] if len(sys.argv) > 4 else "", priority="default")
