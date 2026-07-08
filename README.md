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
