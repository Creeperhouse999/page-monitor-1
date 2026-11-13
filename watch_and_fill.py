"""
Watch a page for a new form link, open it, fill every page of the form with
placeholder answers (first option / one letter), take a full-page screenshot
of each page and push everything to your phone via ntfy.

The form is NEVER submitted: the script only clicks "Next" (Dalej).

Usage:
    python watch_and_fill.py --topic MY_TOPIC                 # watch every 60 s
    python watch_and_fill.py --topic MY_TOPIC --now           # use newest form link already on page
    python watch_and_fill.py --topic MY_TOPIC --url FORM_URL  # skip watching, go straight to a form

Subscribe to the same topic in the ntfy mobile app before running.
"""

import argparse
import base64
import json
import os
import random
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

WATCH_URL = os.environ.get("WATCH_URL", "")  # page to watch: --watch-url or env (encrypted secret in the cloud)
CONTENT_SELECTORS = [".entry-content", "article", "main", "body"]
FORM_HOSTS = ("forms.office.com", "forms.cloud.microsoft", "forms.microsoft.com",
              "docs.google.com/forms", "forms.gle")
CLOSED_TEXTS = ("nie akceptuje odpowiedzi", "not accepting responses",
                "isn't accepting responses", "no longer accepting", "nie przyjmuje już odpowiedzi",
                "nie przyjmuje odpowiedzi", "został zamknięty", "czas na odpowiedzi minął",
                "is closed", "has been closed")
NOT_FOUND_TEXTS = ("nie znaleziono", "not found", "nie można odnaleźć", "nie można znaleźć", "can't find",
                   "cannot find", "couldn't find", "nie istnieje", "doesn't exist", "does not exist",
                   "przepraszamy", "sorry")
START_SELECTORS = ["[data-automation-id=startNowButton]",
                   "button:has-text('Rozpocznij')", "button:has-text('Start now')"]
NEXT_SELECTORS = ["[data-automation-id=nextButton]",
                  "button:has-text('Dalej')", "button:has-text('Next')",
                  "[role=button]:has-text('Dalej')", "[role=button]:has-text('Next')"]
OUT_DIR = Path(__file__).parent / "screenshots"


try:  # cloud runs in UTC; show Polish time in names/logs
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Warsaw")
except Exception:
    TZ = None


def now():
    return datetime.now(TZ) if TZ else datetime.now()


IN_CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))  # public logs: hide links and notification text


def mask(*values):
    """Hide values in GitHub Actions logs (shown as ***)."""
    if IN_CLOUD:
        for v in values:
            if v:
                print(f"::add-mask::{v}", flush=True)


def log(msg):
    print(f"{now():%H:%M:%S} {msg}", flush=True)


def fresh(url):
    """Add a cache-buster so page/CDN caches can't hide a just-published link for minutes."""
    return f"{url}{'&' if '?' in url else '?'}_={int(time.time())}"


# ---------------------------------------------------------------- ntfy
class Ntfy:
    """ntfy client. Options go in the query string so Polish characters / emoji work (UTF-8)."""

    # ntfy.sh anonymous limits (per IP): 2 MB per file, 20 MB attachments per 3 h
    FILE_LIMIT = 1_900_000
    TOTAL_BUDGET = 18_000_000

    # ntfy timestamps have 1 s resolution; messages in the same second can show up in the wrong
    # order on the phone. Keep them > 1 s apart so the order is always right.
    MIN_GAP = 1.1

    def __init__(self, server, topic, title_prefix=""):
        self.url = f"{server.rstrip('/')}/{topic}"
        self.sent_bytes = 0
        self.last_sent = 0.0
        self.prefix = title_prefix  # e.g. "🧪 TEST · " so test runs can't be mistaken for the real thing

    def _send(self, data, method, params):
        wait = self.last_sent + self.MIN_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            return self._send_now(data, method, params)
        finally:
            self.last_sent = time.time()

    def _send_now(self, data, method, params):
        if self.prefix and params.get("title"):
            params = {**params, "title": self.prefix + params["title"]}
        url = self.url + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(url, data=data, method=method)
                urllib.request.urlopen(req, timeout=120).read()
                return True
            except urllib.error.HTTPError as e:
                log(f"[ntfy] send failed: HTTP {e.code} {e.read()[:200]!r}")
                if 400 <= e.code < 500 and e.code != 429:
                    return False  # retrying won't help (e.g. 413 too large)
            except Exception as e:
                log(f"[ntfy] send failed ({e}), attempt {attempt}/3")
            time.sleep(5 * attempt)
        return False

    def text(self, msg, title=None, tags=None, click=None, priority=None):
        log(f"[ntfy] {title}" if IN_CLOUD else f"[ntfy] {title}: {msg}")
        return self._send(msg.encode("utf-8"), "POST",
                          {"title": title, "tags": tags, "click": click, "priority": priority})

    def file(self, path, msg, title=None, tags=None, priority=None, click=None):
        log(f"[ntfy] {title} [{Path(path).name}]" if IN_CLOUD else f"[ntfy] {title}: {msg} [{Path(path).name}]")
        data = Path(path).read_bytes()
        ok = self._send(data, "PUT", {"filename": Path(path).name, "message": msg, "title": title,
                                      "tags": tags, "priority": priority, "click": click})
        if ok:
            self.sent_bytes += len(data)
        return ok

    def budget_left(self):
        return self.TOTAL_BUDGET - self.sent_bytes


def zip_folder(folder, limit=Ntfy.FILE_LIMIT):
    """Zip a run folder (keeping its sub-folders) into parts under ntfy.sh's 2 MB limit.
    Order: pytania.txt, listy_rozwijane/, strony/ – pages were already sent one by one,
    so if the ntfy budget runs out, the parts that matter most went first."""
    order = {"pytania.txt": 0, "listy_rozwijane": 1, "strony": 2}
    files = sorted((f for f in folder.rglob("*") if f.is_file()),
                   key=lambda f: (order.get(f.relative_to(folder).parts[0], 3), str(f)))
    zips, part, size, cur = [], 1, 0, None
    for f in files:
        n = f.stat().st_size
        if cur is None or (size + n > limit and size > 0):
            if cur:
                cur.close()
            path = folder.parent / (f"{folder.name}.zip" if part == 1 else f"{folder.name}_cz{part}.zip")
            cur = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
            zips.append(path)
            part, size = part + 1, 0
        cur.write(f, f"{folder.name}/{f.relative_to(folder).as_posix()}")
        size += n
    if cur:
        cur.close()
    return zips


def question_numbers(scope):
    """Question numbers shown by MS Forms (the dark number badges) inside a page or a question."""
    nums = []
    for t in scope.locator("[data-automation-id=questionOrdinal]").all_inner_texts():
        m = re.search(r"\d+", t)
        if m:
            nums.append(int(m.group()))
    return nums


# ---------------------------------------------------------------- link watching
def is_form(href):
    return any(h in href for h in FORM_HOSTS)


def content_links(page):
    for sel in CONTENT_SELECTORS:
        if page.locator(sel).count():
            return page.eval_on_selector_all(
                f"{sel} a[href]",
                "els => els.map(a => ({href: a.href, text: a.innerText.trim()}))"
                ".filter(l => l.href.startsWith('http'))",
            )
    return []


def load(page, url):
    for attempt in range(5):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return True
        except Exception as e:
            log(f"[load] {url} failed ({e.__class__.__name__}), retry {attempt + 1}/5")
            time.sleep(10)
    return False


class State:
    """Known links, persisted to a JSON file so cloud runs remember what was already there."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.links = set()
        if self.path and self.path.exists():
            self.links = set(json.loads(self.path.read_text(encoding="utf-8")))

    def add(self, *hrefs):
        self.links.update(hrefs)
        if self.path:
            self.path.write_text(json.dumps(sorted(self.links), indent=1), encoding="utf-8")


# ---------------------------------------------------------------- active / daily schedule
STAGES_FILE = Path(__file__).parent / "stages.json"
MONTHS_PL = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6, "lipca": 7,
             "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12}


def parse_page_dates(text):
    """Dates like '16 listopada 2026' written on the watched page."""
    out = set()
    for d, mon, y in re.findall(r"(\d{1,2})\s+(" + "|".join(MONTHS_PL) + r")\s+(\d{4})", text.lower()):
        try:
            out.add(datetime(int(y), MONTHS_PL[mon], int(d)).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return out


class Status:
    """Small persisted status for cloud runs: last daily scan, dates seen on the page, pending link."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.data = {"last_scan": 0, "page_dates": [], "pending": None}
        if self.path and self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, **kw):
        self.data.update(kw)
        if self.path:
            self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")


def stage_windows(status):
    """[(start, end, stage_date)] in local (Polish) time: N days before .. end of N days after each date."""
    cfg = json.loads(STAGES_FILE.read_text(encoding="utf-8")) if STAGES_FILE.exists() else {}
    before, after = cfg.get("days_before", 7), cfg.get("days_after", 1)
    days = {s[:10] for s in cfg.get("stages", [])} | set(status.data.get("page_dates", []))
    wins = []
    for d in sorted(days):
        day = datetime.strptime(d, "%Y-%m-%d")
        wins.append((day - timedelta(days=before), day + timedelta(days=after + 1) - timedelta(seconds=1), day))
    return wins


def stages_cfg():
    return json.loads(STAGES_FILE.read_text(encoding="utf-8")) if STAGES_FILE.exists() else {}


def ignored_links():
    """Links the user chose to skip (stages.json "ignore_links")."""
    return set(stages_cfg().get("ignore_links", []))


def schedule_mode(status):
    """('active'|'daily', human-readable reason)."""
    t = now().replace(tzinfo=None)
    pause = stages_cfg().get("pause_until")
    if pause and t < datetime.strptime(pause[:10], "%Y-%m-%d"):
        return "daily", f"pauza do {pause[:10]} – raz dziennie"
    if status.data.get("pending") and status.data["pending"] not in ignored_links():
        return "active", "nowy link czeka na otwarcie"
    for start, end, day in stage_windows(status):
        if start <= t <= end:
            return "active", f"aktywne okno do {end:%d.%m %H:%M} – co 60 s"
    nxt = [s for s, _, _ in stage_windows(status) if s > t]
    return "daily", ("raz dziennie; następne okno od " + f"{min(nxt):%d.%m.%Y}" if nxt else "raz dziennie")


def auto_watch(page, a, state, status, deadline):
    """Cloud mode. Inside a test window: check every `interval` s. Outside: one check per day,
    sleeping in between (the job then hands over to the next one, so the chain never breaks).
    Returns a new form link, or None when the time budget is used up."""
    def remember_dates():
        try:
            dates = parse_page_dates(page.locator("body").inner_text())
            if dates:
                status.save(page_dates=sorted(dates))
        except Exception:
            pass

    while time.time() < deadline:
        mode, why = schedule_mode(status)
        log(f"[plan] tryb: {'AKTYWNY' if mode == 'active' else 'DZIENNY'} – {why}")
        if mode == "active":
            # watch in 10-min chunks so the plan is re-checked (window may end mid-run)
            link = wait_for_new_form_link(page, a.watch_url, a.interval, False, state,
                                          min(deadline, time.time() + 600))
            remember_dates()
            status.save(last_scan=time.time())
            if not link:
                # never re-check faster than the interval (also at chunk/job boundaries)
                if time.time() + a.interval > deadline:
                    return None
                time.sleep(a.interval)
                continue
        elif time.time() >= status.data["last_scan"] + 86400:
            log("[plan] daily check now")
            link = wait_for_new_form_link(page, a.watch_url, a.interval, False, state, time.time())
            remember_dates()
            status.save(last_scan=time.time())
        else:
            t = now().replace(tzinfo=None)
            starts = [s for s, _, _ in stage_windows(status) if s > t]
            wake = status.data["last_scan"] + 86400
            if starts:
                wake = min(wake, time.time() + (min(starts) - t).total_seconds())
            wake = min(wake, deadline)
            log(f"[plan] next daily check in {(status.data['last_scan'] + 86400 - time.time()) / 3600:.1f} h; "
                f"sleeping until {datetime.fromtimestamp(wake, TZ) if TZ else datetime.fromtimestamp(wake):%d.%m %H:%M}")
            time.sleep(max(1, min(wake - time.time(), 300)))
            continue
        if link:
            status.save(pending=link)  # stays active until this form has been handled
            return link
    return None


def wait_for_new_form_link(page, watch_url, interval, use_existing, state, deadline):
    while not load(page, fresh(watch_url)):
        if time.time() > deadline:
            return None
        time.sleep(interval)
    links = content_links(page)
    if use_existing:
        forms = [l for l in links if is_form(l["href"])]
        if forms:
            log(f"[watch] using existing form link: {forms[0]['href']}")
            return forms[0]["href"]
    if not state.links:  # first ever run: current links are the baseline
        state.add(*(l["href"] for l in links))
    log(f"[watch] {len(state.links)} known links; checking every {interval}s for a new form link")
    while True:
        new = [l for l in links if l["href"] not in state.links]
        new_forms = [l for l in new if is_form(l["href"])]
        if new_forms:
            l = new_forms[0]
            mask(l["href"], l["text"])
            log(f"[watch] NEW form link: {l['text']!r} -> {l['href']}")
            return l["href"]
        for l in new:
            mask(l["href"])
            log(f"[watch] new non-form link ignored: {l['href']}")
            state.add(l["href"])
        if not new:
            log("[watch] no new link")
        if time.time() + interval > deadline:
            return None
        time.sleep(interval)
        if load(page, fresh(watch_url)):
            links = content_links(page)


# ---------------------------------------------------------------- screenshots
def full_shot(page, path, limit=Ntfy.FILE_LIMIT):
    """Full-page JPEG screenshot (also captures inner scroll containers), each file under `limit`.
    Very tall pages that don't fit even at low quality are split into several parts.
    Returns the list of files written."""
    h = page.evaluate(
        """() => Math.max(document.documentElement.scrollHeight,
              ...[...document.querySelectorAll('*')].filter(e => {
                  const s = getComputedStyle(e).overflowY;
                  return (s === 'auto' || s === 'scroll') && e.scrollHeight > e.clientHeight;
              }).map(e => e.scrollHeight + e.getBoundingClientRect().top))"""
    )
    vp = page.viewport_size
    page.set_viewport_size({"width": vp["width"], "height": max(vp["height"], min(int(h) + 50, 16000))})
    page.wait_for_timeout(500)
    try:
        for q in (85, 65, 45):
            page.screenshot(path=str(path), full_page=True, type="jpeg", quality=q)
            if path.stat().st_size <= limit:
                return [path]
        # still too big: split vertically into chunks
        total = page.evaluate("() => document.documentElement.scrollHeight")
        width = page.viewport_size["width"]
        parts, chunk = [], 4000
        for i, y in enumerate(range(0, total, chunk), 1):
            p = path.with_name(f"{path.stem}_cz{i}{path.suffix}")
            for q in (65, 40, 25):
                page.screenshot(path=str(p), full_page=True, type="jpeg", quality=q,
                                clip={"x": 0, "y": y, "width": width, "height": min(chunk, total - y)})
                if p.stat().st_size <= limit:
                    break
            parts.append(p)
        path.unlink(missing_ok=True)
        return parts
    finally:
        page.set_viewport_size(vp)


# ---------------------------------------------------------------- form filling
def letter():
    return random.choice(string.ascii_lowercase)


def text_value(el, numeric=False):
    # hint = everything describing this field: its own attributes + its question's title
    info = el.evaluate(
        """e => {
            const ids = (e.getAttribute('aria-labelledby') || '') + ' ' + (e.getAttribute('aria-describedby') || '');
            const byId = ids.split(/\\s+/).filter(Boolean)
                            .map(i => document.getElementById(i)?.innerText || '').join(' ');
            const q = e.closest('[data-automation-id=questionItem],[role=listitem],fieldset,.form-group,p,li');
            return {type: (e.type || '').toLowerCase(),
                    hint: [e.placeholder, e.getAttribute('aria-label'), e.name, e.id, byId,
                           (e.labels && e.labels[0]) ? e.labels[0].innerText : '',
                           q ? q.innerText.slice(0, 200) : '']
                          .filter(Boolean).join(' ').toLowerCase()};
        }"""
    )
    t, hint = info["type"], info["hint"]
    if t == "email" or "mail" in hint:
        return f"{letter()}{random.randint(10, 99)}@example.com"
    if t == "tel" or any(k in hint for k in ("telefon", "phone", "tel.", "komórk", "mobile")):
        return "500600700"
    if t == "url":
        return "https://example.com"
    if any(k in hint for k in ("kod pocztowy", "postal", "zip")):
        return "81-001"
    if t == "number" or numeric or any(k in hint for k in ("liczb", "number", "wiek ", " age", "how many")):
        return "1"
    if t == "date":
        return "2012-01-15"
    if "yyyy" in hint or "rrrr" in hint:  # MS Forms date picker text field
        return "1/15/2012" if "m/d" in hint else "15.01.2012"
    return letter()


def expanded_listbox_shot(page, lb):
    """Screenshot an open dropdown list with ALL options visible (MS Forms caps it at 256 px + scroll)."""
    h = lb.evaluate("e => e.scrollHeight")
    vp = page.viewport_size
    if h + 40 > vp["height"]:
        page.set_viewport_size({"width": vp["width"], "height": min(h + 40, 15000)})
    lb.evaluate("""e => { const s = e.style;
        s.maxHeight = 'none'; s.height = 'auto'; s.overflow = 'visible';
        s.position = 'fixed'; s.inset = '0px auto auto 0px'; s.transform = 'none'; s.zIndex = '2147483647'; }""")
    page.wait_for_timeout(300)
    return lb.screenshot()


QUESTION_JS = """q => {
  const t = s => (s || '').replace(/\\s+/g, ' ').trim();
  const num = t(q.querySelector('[data-automation-id=questionOrdinal]')?.innerText);
  const title = t(q.querySelector('[data-automation-id=questionTitle], [role=heading]')?.innerText)
                || t((q.innerText || '').split('\\n')[0]);
  const opts = [];
  q.querySelectorAll('input[type=radio], input[type=checkbox], [role=radio], [role=checkbox]').forEach(e => {
    if (e.tagName !== 'INPUT' && e.querySelector('input')) return;
    const l = e.getAttribute('aria-label') || e.getAttribute('data-value')
           || (e.labels && e.labels[0] && e.labels[0].innerText)
           || e.closest('label')?.innerText || e.closest('[data-automation-id=choiceItem]')?.innerText || e.value;
    if (t(l)) opts.push(t(l));
  });
  const kind = q.querySelector('input[type=radio],[role=radio]') ? 'radio'
             : q.querySelector('input[type=checkbox],[role=checkbox]') ? 'checkbox'
             : q.querySelector('[aria-haspopup=listbox],[role=listbox],select') ? 'dropdown'
             : q.querySelector('textarea,input:not([type=hidden])') ? 'text' : 'info';
  return {num, title, kind, opts: [...new Set(opts)]};
}"""
KIND_PL = {"radio": "jednokrotny wybór", "checkbox": "wielokrotny wybór", "dropdown": "lista rozwijana",
           "text": "pole tekstowe", "info": "opis"}
BULLET = {"radio": "○", "checkbox": "☐", "dropdown": "▾"}


def plural_pl(n, one, few, many):
    """Polish plural: 1 opcja, 2-4 opcje, 5+ opcji (12-14 -> many)."""
    if n == 1:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def page_text(page, dropdowns):
    """Readable text of every question on the current page: number, type, every option one per line.
    Dropdown options come from the expanded list, so ones hidden behind the scroll are included."""
    dd_by_num = {d["num"]: d["options"] for d in dropdowns if d.get("num")}
    dd_left = [d["options"] for d in dropdowns if not d.get("num")]
    out = []
    for q in page.locator("[data-automation-id=questionItem], [role=listitem]").all():
        try:
            info = q.evaluate(QUESTION_JS)
        except Exception:
            continue
        if not info["title"]:
            continue
        m = re.search(r"\d+", info["num"] or "")
        num = int(m.group()) if m else None
        title = re.sub(r"^\d+\.?\s*", "", info["title"]) if num else info["title"]
        opts = info["opts"]
        if info["kind"] == "dropdown":
            opts = dd_by_num.get(num) or (dd_left.pop(0) if dd_left else [])
        head = f"Pytanie {num}: " if num else ""
        line = (f"{head}{title}  [{KIND_PL[info['kind']]}"
                + (f", {len(opts)} {plural_pl(len(opts), 'opcja', 'opcje', 'opcji')}]" if opts else "]"))
        bullet = BULLET.get(info["kind"], "-")
        out.append(line + "".join(f"\n    {bullet} {o}" for o in opts))
    return "\n\n".join(out) if out else page.locator("body").inner_text()


def dropdown_sheet(page, items, path, columns=2):
    """Image of dropdown question(s) with the full, expanded option list(s)."""
    cells = ""
    for i, d in enumerate(items, 1):
        imgs = "".join(f'<img src="data:image/png;base64,{base64.b64encode(b).decode()}">'
                       for b in (d["q_png"], d["lb_png"]) if b)
        label = f"Pytanie {d['num']}" if d.get("num") else f"Lista rozwijana {i}"
        cells += f'<div class="c"><div class="n">{label} – wszystkie opcje ({len(d["options"])})</div>{imgs}</div>'
    sheet = page.context.browser.new_page()
    try:
        sheet.set_viewport_size({"width": 1280, "height": 900})
        sheet.set_content(
            "<html><head><meta charset=utf-8><style>body{margin:0;padding:10px;background:#ddd;"
            f"font-family:sans-serif;display:grid;grid-template-columns:repeat({columns},1fr);gap:10px;"
            "align-items:start}"
            ".c{background:#fff;border:2px solid #888;padding:6px}.n{font-weight:bold;margin-bottom:4px}"
            "img{display:block;max-width:100%;margin-bottom:6px;border:1px solid #ccc}</style></head>"
            f"<body>{cells}</body></html>")
        sheet.wait_for_timeout(500)
        return full_shot(sheet, path)
    finally:
        sheet.close()


def fill_page(page, numeric_text=False, dropdowns=None):
    """Fill every field on the page. If `dropdowns` is a list, each dropdown's question
    screenshot, full option list screenshot and option texts are appended to it."""
    n = 0
    # text inputs / textareas -> one letter (or valid email/number/date)
    for el in page.locator(
        "input:not([type]), input[type=text], input[type=email], input[type=tel], "
        "input[type=number], input[type=date], input[type=url], textarea"
    ).all():
        try:
            if el.is_visible() and el.is_editable():
                el.fill(text_value(el, numeric_text))
                n += 1
        except Exception as e:
            log(f"[fill] skip text: {e}")

    # native radios -> first option per group
    done = set()
    for el in page.locator("input[type=radio]").all():
        name = el.get_attribute("name")
        if name in done:
            continue
        try:
            el.check(force=True)
            done.add(name)
            n += 1
        except Exception as e:
            log(f"[fill] skip radio: {e}")

    # native checkboxes -> first one per question
    done = set()
    for el in page.locator("input[type=checkbox]").all():
        key = el.evaluate(
            "e => (e.closest('[data-automation-id=questionItem],[role=group],fieldset') || e.parentElement)"
            ".innerText.slice(0, 120) + '|' + e.name"
        )
        if key in done:
            continue
        try:
            el.check(force=True)
            done.add(key)
            n += 1
        except Exception as e:
            log(f"[fill] skip checkbox: {e}")

    # ARIA radio groups without native inputs (ratings, Google Forms)
    for grp in page.locator("[role=radiogroup]").all():
        if grp.locator("input[type=radio]").count():
            continue
        opts = grp.locator("[role=radio]").all()
        if opts and not grp.locator("[role=radio][aria-checked=true]").count():
            opts[0].click()
            n += 1

    # native selects -> first real option
    for el in page.locator("select").all():
        try:
            vals = [o.get_attribute("value") for o in el.locator("option").all()]
            vals = [v for v in vals if v]
            if el.is_visible() and vals:
                el.select_option(value=vals[0])
                n += 1
        except Exception as e:
            log(f"[fill] skip select: {e}")

    # custom dropdowns (MS Forms / Google Forms) -> capture question + FULL option list, then first option
    vp = page.viewport_size
    for dd in page.locator("[aria-haspopup=listbox], [role=combobox]:not(input), "
                           "[role=listbox]:not([aria-expanded=true])").all():
        try:
            if not dd.is_visible():
                continue
            q = dd.locator(f"xpath=ancestor::*[@data-automation-id='questionItem' or @role='listitem'][1]")
            item = {"title": [], "options": [], "q_png": None, "lb_png": None, "num": None}
            if dropdowns is not None and q.count():
                item["title"] = q.inner_text().split("\n")[0:3]
                item["num"] = (question_numbers(q) or [None])[0]
                q.scroll_into_view_if_needed()
                item["q_png"] = q.screenshot()
            dd.scroll_into_view_if_needed()
            dd.click()
            page.wait_for_timeout(600)
            lb = page.locator("[role=listbox]:visible").last
            opts = [o for o in page.locator("[role=option]:visible").all()
                    if o.get_attribute("aria-disabled") != "true"]
            if dropdowns is not None:
                item["options"] = [o.inner_text().strip() for o in opts]
                if lb.count():
                    item["lb_png"] = expanded_listbox_shot(page, lb)
                dropdowns.append(item)
            # Google Forms lists a "Choose"/"Wybierz" placeholder first
            if opts and opts[0].inner_text().strip().lower() in ("wybierz", "choose", ""):
                opts = opts[1:]
            if opts:
                opts[0].click(force=True)
                n += 1
            else:
                page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception as e:
            log(f"[fill] skip dropdown: {e}")
            page.keyboard.press("Escape")
        finally:
            if page.viewport_size != vp:
                page.set_viewport_size(vp)

    log(f"[fill] filled {n} fields")
    return n


def find_button(page, selectors):
    for sel in selectors:
        btn = page.locator(sel).first
        if btn.count() and btn.is_visible():
            return btn
    return None


ERROR_TEXTS = ("należy odpowiedzieć", "to pytanie jest wymagane", "this is a required question",
               "this question is required", "required question", "answer the following question",
               "wprowadź liczb", "enter a number", "enter a whole number", "liczbę całkowitą",
               "must be a number", "must be between", "musi być", "musi to być", "nieprawidłow", "invalid",
               "please enter", "wprowadź prawidłow", "wprowadź poprawn", "enter a valid")
QUESTION_SEL = "[data-automation-id=questionItem], [role=listitem]"


def value_for_error(err):
    """Pick a value that satisfies the validation message shown under a field."""
    if "mail" in err:
        return f"{letter()}{random.randint(10, 99)}@example.com"
    if any(k in err for k in ("dat", "date", "dd.mm", "mm/dd")):
        return "15.01.2012"
    nums = [int(n) for n in re.findall(r"-?\d+", err)]
    if any(k in err for k in ("między", "between", "zakres", "range", "od ", "from ")) and nums:
        return str(min(nums))
    if any(k in err for k in ("liczb", "number", "numer", "całkowit", "whole", "integer", "cyfr", "digit")):
        return "1"
    if any(k in err for k in ("url", "http", "link")):
        return "https://example.com"
    return "a"


def fix_errors(page, attempt=0):
    """Refill only the questions that currently show a validation error.
    Each retry tries a different kind of value: message-based guess, then a number, then an email."""
    fixed = 0
    for q in page.locator(QUESTION_SEL).all():
        try:
            txt = q.inner_text().lower()
        except Exception:
            continue
        if not any(t in txt for t in ERROR_TEXTS):
            continue
        for el in q.locator("input[type=text], input:not([type]), input[type=number], input[type=email], "
                            "input[type=tel], input[type=url], input[type=date], textarea").all():
            if el.is_visible() and el.is_editable():
                el.fill([value_for_error(txt), "1", f"{letter()}1@example.com"][min(attempt, 2)])
                fixed += 1
        if q.locator("input[type=radio], [role=radio]").count() and not (
                q.locator("input[type=radio]:checked").count()
                or q.locator("[role=radio][aria-checked=true]").count()):
            q.locator("input[type=radio], [role=radio]").first.click(force=True)
            fixed += 1
        if q.locator("input[type=checkbox], [role=checkbox]").count() and not (
                q.locator("input[type=checkbox]:checked").count()
                or q.locator("[role=checkbox][aria-checked=true]").count()):
            q.locator("input[type=checkbox], [role=checkbox]").first.click(force=True)
            fixed += 1
    log(f"[fill] fixed {fixed} fields with validation errors")
    return fixed


def page_signature(page):
    """Identify the current form page by its question titles (validation errors don't change them)."""
    titles = page.eval_on_selector_all(
        "[data-automation-id=questionTitle], [role=listitem] [role=heading]",
        "els => els.map(e => e.innerText.trim()).join('|')")
    if titles:
        return titles
    lines = page.locator("body").inner_text().lower().splitlines()
    return "\n".join(l for l in lines if not any(t in l for t in ERROR_TEXTS))


def click_and_wait(page, btn):
    """Click Next; True only if a different page (different questions) is now shown."""
    before = page_signature(page)
    btn.click()
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1500)
    return page_signature(page) != before


FIELDS_SEL = ("input:not([type=hidden]), textarea, select, [role=radio], [role=checkbox], [role=listbox], "
              "[aria-haspopup=listbox], [data-automation-id=startNowButton]")
FORM_FAST, FORM_SLOW = 5, 60  # seconds between form checks: closed (exists) / not found


def form_state(page, http_status=None):
    """'login' | 'notfound' | 'closed' | 'open' | 'loading' for the page currently shown."""
    url = page.url.lower()
    if "login.microsoftonline.com" in url or "accounts.google.com" in url:
        return "login"
    if "pagenotfound" in url or (http_status and http_status >= 400):
        return "notfound"
    if "closedform" in url:
        return "closed"
    try:
        body = page.locator("body").inner_text().lower()
    except Exception:
        return "loading"
    if any(t in body for t in CLOSED_TEXTS):
        return "closed"
    if page.locator(FIELDS_SEL).count():
        return "open"
    if any(t in body for t in NOT_FOUND_TEXTS):
        return "notfound"
    return "loading"


def probe_form(page, link, timeout=20):
    """Open the form link and decide its state as soon as the page shows it (no fixed waits)."""
    try:
        resp = page.goto(link, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        log(f"[form] load failed ({e.__class__.__name__})")
        return "error"
    code = resp.status if resp else None
    end = time.time() + timeout
    st = "loading"
    while time.time() < end:
        st = form_state(page, code)
        if st != "loading":
            break
        page.wait_for_timeout(400)
    if st == "open":  # let the form finish rendering before filling
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass
        page.wait_for_timeout(1000)
        st = form_state(page, code)
    return "notfound" if st == "loading" else st


def form_closed(page):
    """True while the form isn't usable yet: closed, not found / not published, or empty."""
    return form_state(page) in ("closed", "notfound", "loading")


# ---------------------------------------------------------------- main
def main():
    global STAGES_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default=os.environ.get("NTFY_TOPIC"),
                    help="ntfy topic you subscribed to in the app (or env NTFY_TOPIC)")
    ap.add_argument("--state", help="JSON file remembering known links between runs")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="stop waiting after N minutes (for cloud runs); 0 = forever")
    ap.add_argument("--server", default="https://ntfy.sh")
    ap.add_argument("--watch-url", default=WATCH_URL, help="page to watch for the link")
    ap.add_argument("--interval", type=int, default=60, help="seconds between checks")
    ap.add_argument("--now", action="store_true", help="use newest form link already on the page")
    ap.add_argument("--url", help="skip watching, open this form URL directly")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--auto", action="store_true",
                    help="cloud mode: every 60 s inside active windows (stages.json + dates on the page), "
                         "otherwise one check per day")
    ap.add_argument("--status", help="JSON file for --auto (last daily check, dates seen, pending link)")
    ap.add_argument("--stages", default=str(STAGES_FILE), help="event dates file (default stages.json)")
    ap.add_argument("--form-fast", type=float, default=FORM_FAST,
                    help="seconds between checks while the form exists but is closed (default 5)")
    ap.add_argument("--form-slow", type=float, default=FORM_SLOW,
                    help="seconds between checks while the form link shows 'not found' (default 60)")
    ap.add_argument("--test", action="store_true",
                    help="test run: marks every notification as TEST and says so at the start and end")
    a = ap.parse_args()
    if not a.topic:
        ap.error("--topic or NTFY_TOPIC env is required")
    STAGES_FILE = Path(a.stages)

    ntfy = Ntfy(a.server, a.topic, "🧪 TEST · " if a.test else "")
    state = State(a.state)
    status = Status(a.status)
    mask(a.watch_url, a.url, status.data.get("pending"))
    skip = ignored_links()
    if skip:  # user-skipped links count as already handled
        state.add(*skip)
        if status.data.get("pending") in skip:
            status.save(pending=None)
    if not (a.url or a.watch_url):
        ap.error("--watch-url (or env WATCH_URL) is required")
    deadline = time.time() + a.max_minutes * 60 if a.max_minutes else float("inf")
    # Everything from one run goes into one folder (same layout as the zip sent at the end):
    #   zrzuty_<data>/pytania.txt, strony/strona01_pyt1-5.jpg, listy_rozwijane/pyt53.jpg ...
    run_dir = OUT_DIR / f"zrzuty_{'TEST_' if a.test else ''}{now():%Y%m%d_%H%M%S}"
    pages_dir, dd_dir = run_dir / "strony", run_dir / "listy_rozwijane"
    shots, texts = [], []

    # Links into the private archive repo (files are uploaded there when the run ends).
    archive = os.environ.get("ARCHIVE_BASE", "").rstrip("/")  # e.g. https://github.com/<owner>/page-monitor-data

    def file_link(p):
        return f"{archive}/blob/main/runs/{run_dir.name}/{p.relative_to(run_dir).as_posix()}" if archive else None

    def send(files, title, msg, tags="camera", priority=None):
        for i, p in enumerate(files, 1):
            shots.append(p)
            log(f"[shot] {p.relative_to(run_dir)} ({p.stat().st_size // 1024} KB)")
            part = f" (część {i}/{len(files)})" if len(files) > 1 else ""
            link_txt = f"\n{file_link(p)}" if archive else ""
            if not ntfy.file(p, msg + part + link_txt, title=title, tags=tags, priority=priority,
                             click=file_link(p)):
                ntfy.text(f"{msg}{part}: nie udało się wysłać zrzutu (limit ntfy?). Jest w folderze na końcu.",
                          title=title, tags="warning")

    def shot(page, name, title, msg, tags="camera", priority=None):
        pages_dir.mkdir(parents=True, exist_ok=True)
        send(full_shot(page, pages_dir / f"{name}.jpg"), title, msg, tags, priority)

    def range_txt(nums, word="pyt"):
        if not nums:
            return ""
        return f"{word}{nums[0]}" if nums[0] == nums[-1] else f"{word}{nums[0]}-{nums[-1]}"

    def save_dropdowns(page, step, items):
        """Per dropdown: one image (question + full list) named by question number; plus one sheet."""
        dd_dir.mkdir(parents=True, exist_ok=True)
        for i, d in enumerate(items, 1):
            name = f"pyt{d['num']}" if d.get("num") else f"strona{step:02d}_lista{i:02d}"
            for f in dropdown_sheet(page, [d], dd_dir / f"{name}.jpg", columns=1):
                shots.append(f)
        nums = [d["num"] for d in items if d.get("num")]
        sheet = dropdown_sheet(page, items, dd_dir / f"strona{step:02d}_wszystkie_listy.jpg")
        q = f" (pytania {nums[0]}–{nums[-1]})" if nums else ""
        send(sheet, f"Strona {step} – listy rozwijane 🔽",
             f"Strona {step}: wszystkie opcje z {len(items)} list rozwijanych{q}", tags="arrow_down_small")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not a.headed)
        page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="pl-PL")

        # Silent until the form is actually open: nothing is sent while waiting.
        if a.url:
            link = a.url
        elif a.auto:
            link = auto_watch(page, a, state, status, deadline)
            if not link:
                log("time budget used, exiting (next run continues)")
                browser.close()
                return
        else:
            link = wait_for_new_form_link(page, a.watch_url, a.interval, a.now, state, deadline)
            if not link:
                log("time budget used, exiting (next run continues)")
                browser.close()
                return

        # Open the form. Link on the site but form not ready yet:
        #   exists but closed ("nie akceptuje odpowiedzi") -> retry every 5 s (it may open any second)
        #   not found / 404 (not published yet)            -> retry every 60 s
        # One heads-up when the link is first spotted (never repeated, also not by later runs);
        # after that silent until the form opens.
        slow = False
        announced = status.data.get("announced") == link
        while True:
            if time.time() > deadline:
                log("form not open yet, time budget used, exiting (next run continues)")
                browser.close()
                return
            t0 = time.time()
            st = probe_form(page, link)
            if st in ("closed", "notfound", "error"):
                if not announced and st != "error":
                    if a.test:
                        ntfy.text("To jest TEST – strona testowa, nie prawdziwy formularz.",
                                  title="Początek testu", tags="test_tube")
                    why = "jeszcze zamknięty" if st == "closed" else "jeszcze nieopublikowany (nie znaleziono)"
                    every = "5 s" if st == "closed" else "60 s"
                    ntfy.text(f"Link pojawił się na stronie: {link}\nFormularz {why} – sprawdzam co {every} "
                              "i wyślę zrzuty, gdy tylko się otworzy.",
                              title="Link znaleziony 👀 (czekam na otwarcie)", tags="eyes", click=link,
                              priority="high")
                    status.save(announced=link)
                    announced = True
                slow = st == "notfound" or (slow and st == "error")
                wait = max(0.5, (a.form_slow if slow else a.form_fast) - (time.time() - t0))
                log(f"[form] {st} – next check in {wait:.0f}s")
                time.sleep(wait)
                continue
            if st == "login":
                if a.test:
                    ntfy.text("To jest TEST – strona testowa, nie prawdziwy formularz.",
                              title="Początek testu", tags="test_tube")
                ntfy.text(f"Link znaleziony! {link}", title="Link znaleziony! 🔗",
                          tags="link", click=link, priority="high")
                shot(page, "logowanie", "Wymagane logowanie 🔒",
                     "Formularz wymaga zalogowania – nie mogę go wypełnić. Otwórz link ręcznie.",
                     tags="lock", priority="high")
                state.add(link)
                status.save(pending=None)
                browser.close()
                return
            break  # open
        if a.test and not announced:
            ntfy.text("To jest TEST – strona testowa, nie prawdziwy formularz.\n"
                      "Wszystkie powiadomienia z tego przebiegu mają w tytule „🧪 TEST”.",
                      title="Początek testu", tags="test_tube")
        if announced:
            ntfy.text(f"Formularz jest OTWARTY: {link}\nOtwieram i robię zrzuty...",
                      title="Formularz otwarty! 🔓", tags="unlock", click=link, priority="high")
        else:
            ntfy.text(f"Link znaleziony! {link}\nOtwieram...", title="Link znaleziony! 🔗",
                      tags="link", click=link, priority="high")

        start = find_button(page, START_SELECTORS)
        if start:
            shot(page, "strona00_start", "Strona startowa 📸", "Strona startowa – klikam „Rozpocznij”...")
            texts.append("===== STRONA STARTOWA =====\n" + page.locator("body").inner_text())
            click_and_wait(page, start)

        pages_done, stuck = 0, False
        for step in range(1, 101):  # walk every page of the form
            dropdowns = []
            fill_page(page, dropdowns=dropdowns)
            nums = question_numbers(page)
            name = f"strona{step:02d}" + (f"_{range_txt(nums)}" if nums else "")
            q = f" (pytania {nums[0]}–{nums[-1]})" if len(nums) > 1 else (f" (pytanie {nums[0]})" if nums else "")
            shot(page, name, f"Strona {step} 📸", f"Strona {step}{q}")
            # extras must never stop the walk through the form
            if dropdowns:
                try:
                    save_dropdowns(page, step, dropdowns)
                except Exception as e:
                    log(f"[dropdowns] failed: {e}")
                    ntfy.text(f"Strona {step}: nie udało się zrobić zrzutów list rozwijanych ({e.__class__.__name__}). "
                              "Opcje są w pytania.txt.", title=f"Strona {step} – listy rozwijane", tags="warning")
            try:
                texts.append(f"===== STRONA {step}{q} =====\n{page_text(page, dropdowns)}")
            except Exception as e:
                log(f"[text] failed: {e}")
            pages_done = step
            nxt = find_button(page, NEXT_SELECTORS)
            if not nxt:
                break  # last page: NEVER click submit
            if click_and_wait(page, nxt):
                continue
            # blocked by validation: fix the flagged fields and retry a few times
            ntfy.text(f"Nie mogę przejść dalej ze strony {step}, ponawiam...",
                      title="Ponawiam ⚠️", tags="warning")
            moved = False
            for attempt in range(3):
                if not fix_errors(page, attempt):
                    fill_page(page, numeric_text=True)
                nxt = find_button(page, NEXT_SELECTORS)
                if nxt and click_and_wait(page, nxt):
                    moved = True
                    break
            if moved:
                ntfy.text(f"Przeszedłem dalej ✅ (strona {step + 1})", title="Przeszedłem dalej",
                          tags="white_check_mark")
                continue
            shot(page, f"strona{step:02d}_blad", f"Utknąłem na stronie {step} ❌",
                 f"Nie da się przejść dalej ze strony {step}. Zobacz zrzut – możesz dokończyć ręcznie: {link}",
                 tags="x", priority="high")
            stuck = True
            break

        browser.close()

    state.add(link)  # handled: don't process this link again
    status.save(pending=None)
    n = pages_done
    summary = (f"Zrobiłem zrzuty {n} {plural_pl(n, 'strony', 'stron', 'stron') if n != 1 else 'strony'}"
               + (" (utknąłem po drodze)" if stuck else "")
               + ". Formularz NIE został wysłany.")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pytania.txt").write_text(
        f"Formularz: {link}\nZapisano: {now():%Y-%m-%d %H:%M}\n{summary}\n\n" + "\n\n".join(texts),
        encoding="utf-8")
    n_dd = len(list(dd_dir.glob("pyt*.jpg"))) + len(list(dd_dir.glob("strona*_lista*.jpg"))) if dd_dir.exists() else 0
    ntfy.text(f"Wszystkie zrzuty ekranu zrobione! 🎉\n{summary}\n"
              + (f"Listy rozwijane: {n_dd} (osobny folder).\n" if n_dd else "")
              + "Wysyłam folder...", title="Gotowe! 🎉", tags="tada")
    zips = zip_folder(run_dir)  # zip goes to ntfy only (never to GitHub)
    for i, z in enumerate(zips, 1):
        part = f" – część {i}/{len(zips)}" if len(zips) > 1 else ""
        if z.stat().st_size > ntfy.budget_left():
            ntfy.text(f"Reszty folderu nie wysłałem{part} – limit ntfy (20 MB na 3 h). "
                      "Wszystkie strony masz w powiadomieniach wyżej.", title="Folder ze zrzutami 📁",
                      tags="warning")
            break
        ntfy.file(z, f"Folder (zip): strony/, listy_rozwijane/, pytania.txt{part}",
                  title="Folder ze zrzutami (zip) 📁", tags="file_folder")
    if archive:
        # Cloud: the workflow uploads the folder (no zip) to the private repo, THEN sends the
        # GitHub folder link (works immediately) and, for test runs, "Koniec testu" as the last message.
        log(f"done: {run_dir} (GitHub folder link + end message follow from the workflow)")
        return
    if a.test:
        ntfy.text("Koniec testu. To był TEST – prawdziwa strona jest nadal obserwowana.",
                  title="Koniec testu", tags="test_tube")
    log(f"done: {run_dir}")


if __name__ == "__main__":
    main()
