# page-monitor

A small website monitor that runs on GitHub Actions.

It watches one web page for a newly published form link. When the form becomes available it
opens it, walks through every page, takes full-page screenshots and sends them as push
notifications via [ntfy](https://ntfy.sh). It never submits anything.

## How it works

- **Check frequency:** every 60 s inside configured active windows, otherwise once a day.
- **Form not ready yet:** retried every 5 s while it exists but is closed, every 60 s while it
  returns "not found".
- **Capture:** full-page screenshot of every page, all options of every dropdown (expanded),
  and a plain-text dump of all questions/options. Runs are bundled into a zip.
- **Notifications:** sent in order, ≥ 1.1 s apart; attachments kept under ntfy's 2 MB limit.

## Reliability

The same code runs in a ring of repositories (`page-monitor-1` … `page-monitor-10`).

- Only one repository is active at a time; each run starts the next one when it ends.
- Every repository runs a **sentinel** that checks the ring every 2 minutes. If nothing is
  active, the next repository takes over (staggered delays, looping round).
- A weekly keep-alive prevents GitHub from disabling the schedules.

| Notification | Meaning |
|---|---|
| ⚠️ Obserwator przestał działać | nothing was active; another repository is taking over |
| Obserwator działa ✅ | active again |
| 🚨 Obserwator nie startuje | a repository could not start for 10 minutes; the next one takes over |

## Configuration (not in this repository)

| Where | What |
|---|---|
| Secret `WATCH_URL` | page to watch |
| Secret `NTFY_TOPIC` | ntfy topic for notifications |
| Secret `DATA_SSH_KEY` | deploy key for the private data repository |
| Private repo `page-monitor-data` | `stages.json` (active windows), state, saved runs |

## Run locally

```
pip install playwright && python -m playwright install chromium
python watch_and_fill.py --topic YOUR_TOPIC --watch-url https://example.com/page
python watch_and_fill.py --topic YOUR_TOPIC --url https://forms.office.com/...   # one form directly
```

Add `--test` to mark all notifications as a test run.
