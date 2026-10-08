# SafeGuide drug alerts — cloud scraper

Runs the [Victorian Pill Testing Service](https://www.vicpilltesting.org.au/drug-notifications)
drug-notification scraper in GitHub's cloud (independent of any local machine) and
deploys the result to the live SafeGuide app on IONOS **only when it changes**.

- **Scraper:** [`alerts/fetch_notifications.py`](alerts/fetch_notifications.py) — stdlib-only
  Python + the `pdftotext` CLI (poppler-utils). Deterministic, no LLM. Only fetches the
  public `/drug-notifications` page and the public `/s/*.pdf` files (robots-allowed).
- **Output:** [`staging-site/alerts/alerts.json`](staging-site/alerts/alerts.json) — consumed
  by the SafeGuide app at `app.veaea.org/harm-reduction/alerts/`.
- **Automation:** [`.github/workflows/alerts.yml`](.github/workflows/alerts.yml) — runs twice
  daily (+ manual dispatch), deploys changed `alerts.json` to IONOS over SSH-key SFTP,
  verifies the live URL returns 200, and commits the refreshed JSON back as an audit trail.
- **Multi-region feed:** [`alerts/fetch_all_sources.py`](alerts/fetch_all_sources.py) +
  [`.github/workflows/alerts-multi.yml`](.github/workflows/alerts-multi.yml) — The Know's
  national aggregate (`alerts-multi.json`), deployed to production (and a dev copy). The app
  page merges it with `alerts.json`.
- **Heartbeat:** both workflows upload `status.json` / `status-multi.json`
  ([`alerts/heartbeat.py`](alerts/heartbeat.py)) on every successful run, changed or not.
  The feeds' `generated_at` only moves when alerts change, so the page uses the heartbeat to
  tell "quiet source" from "stalled pipeline". Both workflows also redeploy a feed that prod
  no longer serves, and use `lftp` `cmd:fail-exit` so a failed upload fails the run.
- **Watchdog:** [`.github/workflows/watchdog.yml`](.github/workflows/watchdog.yml) +
  [`alerts/check_heartbeat.py`](alerts/check_heartbeat.py) run every 6 h and fail if a heartbeat is
  older than 36 h or production stops serving a feed, so a stall reaches a human (GitHub emails the
  person who last edited the schedule). A quiet source is never a failure.
- **The Know coverage:** the parser reads the whole archive and keeps 270 days plus each
  jurisdiction's latest 2 (up to ~3 years). QLD Health and NT Health block scripted access behind a
  Cloudflare challenge and are not scraped; The Know republishes them. See
  `alerts/config/alert-sources.json` (`meta.verification_2026_10_08`).

## Secret

One repository secret is required:

| Secret | What |
| --- | --- |
| `SFTP_KEY` | Dedicated CI SSH **private** key (ed25519). Its public half is registered under IONOS → SSH access for the webspace user. Used only to upload `alerts.json`. |

Host / user / remote path are non-secret and set as `env:` in the workflow.

## Running locally

```sh
python3 alerts/fetch_notifications.py   # needs `pdftotext` on PATH (brew install poppler)
```
