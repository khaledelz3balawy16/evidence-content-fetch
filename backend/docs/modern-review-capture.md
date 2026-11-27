# Aptem Modern capture and comparison

Run from this project's `backend` directory:

```powershell
python manage.py capture_modern_reviews --workers 3 --aptem-learner-ids 106,16098,3270,4907 --verify-ui
python manage.py capture_modern_reviews --workers 3 --verify-ui
```

The first command checks the agreed sample; the second collects all canonical
learners. Each invocation gets a fresh `debug/modern-reviews/<UTC-run>/` folder
under this project. It performs a read-only Postgres snapshot, read-only Aptem
requests, and local file writes. It does not start a scheduler or apply DB changes.

`run-status.json` records the runner PID, collector PID, current phase and latest
progress. `phase-history.jsonl` appends each status without replacing past events.
`capture.log` records collector output; `capture/progress.jsonl` records every
learner and worker number. A single Node process runs 1–4 async workers, default 3.

On interruption, resume the same immutable candidate inventory and retry only
failed/incomplete captures (within 24 hours):

```powershell
python -u manage.py capture_modern_reviews --resume --output ../debug/modern-reviews/RUN --workers 3 --verify-ui
```

Progress-file replacement retries Windows sharing violations. A failed worker
stops the queue and waits for in-flight workers before closing the browser.

The collector authenticates using `backend/.env` without printing credentials.
It uses this project's `.playwright-runtime/playwright/driver` Node/Playwright
installation and installed Chrome; it never imports another project.

Both Planned and Finished are paginated using the Modern grid routes. Enrollment
is excluded with its IDs and original pagination counts preserved in evidence.
Programme IDs do not replace observed membership. Missing current programmes or
failed learner fetches do not prove that historical reviews are absent. Classic
documents and signer identities are matched to the exact review; ambiguity stays
unresolved.

The generated `stage/` folder contains a verified SQLite inventory, raw learner
evidence, HTML dashboard, CSV comparisons and checksums. Apply accepts this stage
through `apply_modern_review_stage`; capture itself never writes shared data.
Before applying, examine `stage/stage-summary.json` and `stage/report.json`.

## One complete local cycle

```powershell
python -u manage.py run_modern_reviews_cycle --workers 3 --verify-ui --apply
```

This uses the Modern collector, not the older `run_cycle` all-OData importer.
It has no scheduler or deployment step. Omitting `--apply` performs a dry run.
Do not replace the server task until the team explicitly releases this command.

To apply or resume a previously validated run:

```powershell
python -u manage.py apply_modern_review_stage --stage ../debug/modern-reviews/RUN/stage --backup ../debug/modern-reviews/RUN/recovery/before-apply.json --progress ../debug/modern-reviews/RUN/apply-progress.json --apply
# Add --resume to reuse that exact stage and recovery file after interruption.
python manage.py audit_modern_review_stage --stage ../debug/modern-reviews/RUN/stage --report ../debug/modern-reviews/RUN/readback.json
```

Each learner commits independently after both tabs, current IDs and Classic
metadata pass exact readback. A batched UPDATE changes the two metadata keys
together. Equal values are skipped. Successful empty lists can archive old
membership; failed/no-current-program captures cannot. The operation retains all
review rows, sections and foreign-key references. Source and listing programme
IDs remain distinct; missing source programme IDs remain unknown.

`apply-progress.json` counts committed learners. A brief `SyncRep` observation is
not proof of a stalled operation. If a COMMIT loses its connection, its outcome
is unconfirmed until exact readback on resume. From the project root, watch with:

```powershell
.\scripts\watch_modern_reviews.ps1
```

## Recovery

The verified main backup and `before-apply.learners/` journals preserve only the
metadata this operation owns. They are not full content backups and must never
be used to justify physical review deletion. Each learner journal contains its
exact before/after values and inserted IDs, durably written before commit.

```powershell
python manage.py restore_modern_review_stage --journal ../debug/modern-reviews/RUN/recovery/before-apply.learners/learner-ID-1.json
```

This is read-only by default. To restore, add `--apply --backup NEW_PATH.json`.
Recovery refuses later metadata changes, preserves unrelated keys, and retains
inserted review rows. Restore journals newest first if a learner has multiple
attempts. An uncommitted journal is detected as already restored.

## Classic document ordering

The collector requests `CreateDateTime-desc` from Aptem and retains the server
order alongside exact document IDs and original date values. Latest selection
can use this proof without inventing timezone offsets for older versions. Tied
latest dates, mismatched version sets and ambiguous groups stay unresolved.
Missing-signature roles still require exact learner/review/document proof and
agreement between the signature grid and its Classic group.

The report's current list is available locally in `stage/dashboard.html`.
Database metadata changes alone do not deploy a consumer's display filter.
