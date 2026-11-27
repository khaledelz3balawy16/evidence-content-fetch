# Nightly sync on the VPS (00:00 every night)

The nightly job replaces the Windows Task Scheduler scripts (`scripts/*.ps1`).
It runs the same pipeline described in `modern-review-capture.md`, unattended:

| Step | Command | Writes |
|------|---------|--------|
| 1 | `run_modern_reviews_cycle --workers 3 --apply` | read-only Aptem capture -> `debug/modern-reviews/<run>/` -> per-learner apply to `"Learner".reviews` |
| 2 | `process_source_evidence --all` | downloads new evidence to `APTEM_DOWNLOAD_DIRECTORY`, fills `AptemEvidence` |

Both steps run even if the other fails. A `flock` guarantees only one run at a
time, so a slow night can never overlap the next one (the old Windows setup
started two cycles a second apart, and they failed each other's apply).

## Install (once)

Ubuntu 22.04+/Debian 12+ with Python 3.12+ (Ubuntu 24.04 recommended).

```bash
# 1. Put the code on the server (as your normal, non-root user)
git clone <repo-url> ~/evidence-content-fetch
cd ~/evidence-content-fetch

# 2. Copy the secrets file from the old machine (it is not in git)
#    from the old machine:  scp backend/.env user@VPS:~/evidence-content-fetch/backend/.env

# 3. Install everything and enable the timer
sudo NIGHTLY_TIMEZONE=Europe/London ./deploy/install_vps.sh
```

`NIGHTLY_TIMEZONE` decides which midnight "00:00" means (default
`Europe/London`, which follows BST/GMT automatically). Use e.g. `Africa/Cairo`
or `UTC` if you prefer.

The installer:

- creates `.venv` and installs `backend/requirements.txt`;
- installs Google Chrome (amd64) or Playwright Chromium (arm64, and sets
  `APTEM_BROWSER_CHANNEL=` in `.env`);
- links `.playwright-runtime/playwright` to the pip Playwright package (the
  collector loads `driver/node` and `driver/package` from there);
- checks Django can reach Neon (`manage.py check` + `showmigrations`);
- installs and enables `aptem-nightly.timer`.

It does **not** run migrations or call Aptem.

## Test it right away

```bash
sudo systemctl start aptem-nightly.service   # full run now (takes ~20-40 min)
journalctl -u aptem-nightly.service -f       # watch
```

Or a dry run of step 1 only, without writing to the database:

```bash
cd backend && ../.venv/bin/python -u manage.py run_modern_reviews_cycle --workers 3 \
    --aptem-learner-ids 106,16098,4907
```

## Day to day

```bash
systemctl list-timers aptem-nightly.timer        # next / last run
systemctl status aptem-nightly.service           # last result (exit code)
ls -t logs/nightly_*.log | head                  # one UTF-8 log per run
sudo systemctl disable --now aptem-nightly.timer # pause the schedule
```

Each run also leaves its evidence in `debug/modern-reviews/<UTC-run>/`
(`run-status.json`, `apply-progress.json`, `recovery/`). Those folders are the
recovery journals and are never deleted automatically; prune old ones by hand
if disk space gets tight.

## Tuning

`sudo systemctl edit aptem-nightly.service`, then add for example:

```ini
[Service]
Environment=NIGHTLY_WORKERS=4
Environment=NIGHTLY_VERIFY_UI=1
Environment=NIGHTLY_RUN_EVIDENCE=0
Environment=NIGHTLY_EVIDENCE_LIMIT=500
Environment=NIGHTLY_LOG_KEEP_DAYS=30
```

To change the time, re-run the installer with another `NIGHTLY_TIMEZONE`, or
edit `OnCalendar=` in `/etc/systemd/system/aptem-nightly.timer` and run
`sudo systemctl daemon-reload`.

## Updating the code

```bash
cd ~/evidence-content-fetch && git pull
.venv/bin/pip install -r backend/requirements.txt   # if requirements changed
```

The timer picks up the new code on the next run; no restart is needed.

## Cutting over from Windows

Turn the Windows tasks off before the first VPS night, otherwise both machines
reconcile the same learners at once:

```powershell
Get-ScheduledTask | Where-Object TaskName -like 'KBC*' | Disable-ScheduledTask
```

(Also check Task Scheduler for any task that runs `run_modern_cycle.ps1`.)

## Without systemd (cron)

```cron
CRON_TZ=Europe/London
0 0 * * * /home/USER/evidence-content-fetch/scripts/nightly_sync.sh >/dev/null 2>&1
```
