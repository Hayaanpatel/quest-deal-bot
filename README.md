# quest-deal-bot

Watches UK retailers for a Meta Quest 3S 256GB deal and pushes an alert to your phone via [ntfy](https://ntfy.sh). Runs free on GitHub Actions every 30 minutes.

- Setup: install the ntfy app, subscribe to your topic, then `gh secret set NTFY_TOPIC --body <topic>`.
- Edit `config.json` to change targets, `target_price` (default 299) and `drop_percent` (default 10).
- Local: `python3 watcher.py --once --dry-run`, `--test-notify`, `--loop 30`. Tests: `python3 -m unittest discover -s tests`.
- Alerts fire when price <= target or >= `drop_percent` below the highest price seen, never when JSON-LD says OutOfStock, and only on a new low per URL.
- Note: GitHub pauses scheduled workflows after 60 days without repo activity.
