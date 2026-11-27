"""Reconcile a verified Modern inventory in recoverable learner transactions.

On existing reviews only the two reconciliation metadata keys are changed, plus
the `status` and `completed_date` columns, which follow `aptem_modern` in the
same learner transaction (see `column_writes`). Historical rows/content are
retained. No Aptem calls or physical deletion exist.
"""
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import connection, transaction

from .review_api_sync import ReviewSyncError, display_status
from .review_signatures import load_verified, write_verified


MEMBERSHIP_KEY = 'aptem_modern'
DOCUMENT_KEY = 'aptem_classic_document'
INSTANCE = 'kentbusinesscollege.aptem.co.uk'
# Modern dates are 'source_wall_clock': Aptem's UK local time without an offset.
SOURCE_TIMEZONE = ZoneInfo('Europe/London')


class StageApplyError(ReviewSyncError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def _id(value, label='ID'):
    value = str(value or '')
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError(f'{label} must be a positive numeric ID')
    return str(int(value))


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _stamp(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Membership timestamps require an explicit timezone')
    return parsed


def _optional_id(value):
    return None if value is None else _id(value, 'source program ID')


def _validate_membership(value, learner, review):
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or value.get('source') != 'modern_planned_finished'
            or value.get('instance') != INSTANCE or value.get('verified') is not True
            or value.get('both_tabs_complete') is not True or value.get('current') is not True
            or value.get('tab') not in ('planned', 'finished') or not value.get('run_id')):
        raise ValueError('Stage current review lacks verified membership')
    if (_id(value.get('aptem_learner_id')) != learner
            or _id(value.get('aptem_review_id')) != review):
        raise ValueError('Stage membership identity mismatch')
    _id(value.get('listing_program_id'), 'listing program ID')
    _optional_id(value.get('source_program_id'))
    _stamp(value.get('checked_at'))


def load_stage(path):
    """Read the checksum-verified stage, including proof for empty inventories."""
    path = Path(path).resolve()
    checksums = json.loads((path / 'checksums.json').read_text(encoding='utf-8'))
    if not {'reviews.sqlite3', 'stage-summary.json'} <= checksums.keys():
        raise ValueError('Stage is missing required checksums')
    for name, digest in checksums.items():
        target = (path / name).resolve()
        if not target.is_relative_to(path) or _checksum(target) != digest:
            raise ValueError(f'Stage checksum mismatch: {name}')
    summary = json.loads((path / 'stage-summary.json').read_text(encoding='utf-8'))
    if (summary.get('mode') != 'live_capture_local_staging'
            or summary.get('exact_current_ids') is not True
            or summary.get('sqlite_integrity') != 'ok' or summary.get('validation_errors')
            or summary.get('shared_database_writes') != 0 or summary.get('deletions') != 0):
        raise ValueError('Stage is not a complete read-only verified inventory')
    db = sqlite3.connect((path / 'reviews.sqlite3').as_uri() + '?mode=ro', uri=True)
    try:
        learners = {str(learner): {'lms_learner_id': str(lms or ''), 'status': status,
                                    'checked_at': checked}
                    for learner, lms, status, checked in db.execute('SELECT * FROM learners')}
        rows = []
        for learner, review, stored_id, source, metadata in db.execute('''SELECT
                aptem_learner_id,aptem_review_id,existing_local_id,review_json,metadata_json
                FROM current_reviews'''):
            learner, review = _id(learner), _id(review)
            meta, raw = _json(metadata), _json(source)
            membership, document = meta.get(MEMBERSHIP_KEY), meta.get(DOCUMENT_KEY)
            _validate_membership(membership, learner, review)
            if (_id(raw.get('Id')) != review or _id(raw.get('UserId')) != learner
                    or _optional_id(raw.get('ProgramId')) != _optional_id(membership.get('source_program_id'))):
                raise ValueError('Stage raw review identity/program disagrees with membership')
            if ('enrol' in str(raw.get('TypeName', '')).lower()
                    or not raw.get('TypeName') or raw.get('Status') != membership.get('source_status')
                    or raw.get('PlannedStartDateTime') != membership.get('planned_at')
                    or raw.get('CompleteDateTime') != membership.get('completed_at')):
                raise ValueError('Stage source type, status or dates disagree with membership')
            if not isinstance(document, dict):
                raise ValueError('Stage document evidence is missing')
            for evidence in (document.get('selection'), document.get('evidence')):
                if (not isinstance(evidence, dict) or _id(evidence.get('aptem_learner_id')) != learner
                        or _id(evidence.get('aptem_review_id')) != review):
                    raise ValueError('Stage document identity mismatch')
            if learners.get(learner, {}).get('status') != 'ok':
                raise ValueError('Current review belongs to an unsuccessful inventory')
            sample = learners[learner].setdefault('membership', membership)
            for key in ('run_id', 'checked_at', 'listing_program_id'):
                if sample[key] != membership[key]:
                    raise ValueError('One learner has conflicting inventory provenance')
            rows.append({'learner_id': learner, 'review_id': review,
                         'stored_id': str(stored_id) if stored_id is not None else None,
                         'source': raw, 'membership': membership, 'document': document})
    finally:
        db.close()
    if len({(r['learner_id'], r['review_id']) for r in rows}) != len(rows):
        raise ValueError('Stage contains duplicate Modern identities')
    for name in ('staged_current_reviews', 'source_reviews'):
        if name in summary and summary[name] != len(rows):
            raise ValueError('Stage review total disagrees with verified summary')
    # A successful learner can have zero reviews. Only signed capture evidence
    # for BOTH empty tabs permits historical marking in this case.
    empty = {key for key, value in learners.items()
             if value['status'] == 'ok' and 'membership' not in value}
    if empty:
        if 'live.jsonl' not in checksums:
            raise ValueError('Empty inventory lacks checksum-verified tab evidence')
        with (path / 'live.jsonl').open(encoding='utf-8') as source:
            for line in source:
                capture = json.loads(line)
                learner = str(capture.get('aptem_id'))
                if learner not in empty:
                    continue
                if (capture.get('status') != 'ok'
                        or str(capture.get('lms_learner_id')) != learners[learner]['lms_learner_id']):
                    raise ValueError('Empty inventory learner mapping mismatch')
                for tab in ('planned', 'finished'):
                    bucket = capture.get('modern_buckets', {}).get(tab, {})
                    pages = bucket.get('pages')
                    if (bucket.get('complete') is not True or bucket.get('items') != []
                            or bucket.get('total') != 0 or not pages
                            or any(p.get('http_status') != 200 or p.get('page') != i + 1
                                   or p.get('count') != 0 for i, p in enumerate(pages))):
                        raise ValueError('Empty inventory has incomplete tab evidence')
                _stamp(capture['membership_checked_at'])
                learners[learner]['membership'] = {
                    'schema_version': 1, 'source': 'modern_planned_finished', 'instance': INSTANCE,
                    'listing_program_id': _id(capture['aptem_program_id']),
                    'run_id': capture['run_id'], 'checked_at': capture['membership_checked_at']}
        if any('membership' not in learners[key] for key in empty):
            raise ValueError('Empty inventory capture is missing')
    return summary, learners, rows


def _status_key(value):
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def column_status(membership):
    """`status` column value for Aptem's source_status; None means keep the column."""
    value = str((membership or {}).get('source_status') or '').strip()
    if not value:
        return None
    if _status_key(value) in ('complete', 'completed'):
        return 'Completed'
    if _status_key(value) == 'awaitingsignature':
        return 'Awaiting Signature'
    return display_status(value)


def column_completed(membership):
    """`completed_date` instant (UTC) for Aptem's completed_at; None means keep the column."""
    membership = membership or {}
    value = membership.get('completed_at')
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        if membership.get('date_semantics') != 'source_wall_clock':
            raise ValueError('completed_at has no offset and no source_wall_clock semantics')
        parsed = parsed.replace(tzinfo=SOURCE_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def _instant(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def column_writes(previous, membership):
    """(status, completed_date) to write; None = keep. Equal values are never rewritten,
    so the "Feedback" outbox trigger only fires for real changes. Formatting-only status
    differences (AwaitingSignature/Awaiting Signature) count as equal, and completed_date
    is compared to the second because Aptem Modern lists whole seconds."""
    previous = previous or {}
    status = column_status(membership)
    if status is not None and _status_key(status) == _status_key(previous.get('status_before')):
        status = None
    completed = column_completed(membership)
    before = _instant(previous.get('completed_date_before'))
    if completed is not None and before is not None and before.replace(microsecond=0) == completed:
        completed = None
    return status, completed.isoformat() if completed else None


def _columns_match(status, completed_date, membership):
    return column_writes({'status_before': status, 'completed_date_before': completed_date},
                         membership) == (None, None)


def _historic_marker(learner_id, review_id, sample):
    return {'schema_version': sample['schema_version'], 'source': sample['source'],
        'instance': sample['instance'], 'aptem_learner_id': learner_id,
        'aptem_review_id': review_id, 'run_id': sample['run_id'],
        'checked_at': sample['checked_at'], 'verified': True,
        'both_tabs_complete': True, 'current': False,
        'listing_program_id': sample['listing_program_id'], 'tab': None}


def _protect_newer(existing, incoming):
    before = existing.get('modern_before')
    if not isinstance(before, dict):
        return
    incoming_at = _stamp(incoming['checked_at'])
    attempts = [before.get('checked_at')]
    if isinstance(before.get('last_attempt'), dict):
        attempts.append(before['last_attempt'].get('checked_at'))
    if any(stamp and _stamp(stamp) > incoming_at for stamp in attempts):
        raise ValueError('Incoming inventory is older than stored membership or its latest attempt')
    old_program, new_program = before.get('source_program_id'), incoming.get('source_program_id')
    if incoming.get('current') and old_program is not None and new_program is not None:
        if _id(old_program) != _id(new_program):
            raise ValueError('Review source program changed; exact identity needs review')


def plan_stage(learners, stage_rows, stored_rows):
    """Pure exact-identity plan. Unsuccessful/Enrollment history is untouched."""
    staged = {(row['learner_id'], row['review_id']): row for row in stage_rows}
    if len(staged) != len(stage_rows):
        raise ValueError('Stage contains duplicate Modern identities')
    samples = {learner: value['membership'] for learner, value in learners.items()
               if value.get('status') == 'ok' and value.get('membership')}
    for row in stage_rows:
        if learners.get(row['learner_id'], {}).get('status') != 'ok':
            raise ValueError('Current membership belongs to an unsuccessful inventory')
        samples.setdefault(row['learner_id'], row['membership'])
    stored = {}
    for row in stored_rows:
        key = _id(row['aptem_learner_id']), _id(row['aptem_review_id'])
        if key in stored:
            raise ValueError('Stored duplicate Aptem review identity needs consolidation')
        stored[key] = row
    changes = []
    for key, row in staged.items():
        previous = stored.get(key)
        if previous:
            _protect_newer(previous, row['membership'])
        status, completed = column_writes(previous, row['membership'])
        same = (previous and previous.get('modern_before') == row['membership']
                and previous.get('document_before') == row['document']
                and status is None and completed is None)
        changes.append({'kind': 'unchanged' if same else ('update' if previous else 'insert'),
                        'identity': key, 'local_id': previous['id'] if previous else None,
                        'membership': row['membership'], 'document': row['document'], 'source': row['source'],
                        'status': status, 'completed_date': completed})
    for key, previous in stored.items():
        learner, review = key
        if (learner in samples and key not in staged
                and 'enrol' not in str(previous.get('review_type', '')).lower()):
            marker = _historic_marker(learner, review, samples[learner])
            _protect_newer(previous, marker)
            # A marker has no source_status/completed_at, so both columns are kept.
            changes.append({'kind': 'unchanged' if previous.get('modern_before') == marker else 'historical',
                            'identity': key, 'local_id': previous['id'], 'membership': marker,
                            'document': None, 'source': None, 'status': None, 'completed_date': None})
    return changes


def _review_payload(source):
    # TypeName is Aptem Modern's actual type; ReviewType is not supplied there.
    return {'Id': source['Id'], 'LearnerId': source['UserId'], 'Name': source.get('Name'),
            'Type': source['TypeName'], 'OwnerName': source.get('ReviewerName'),
            'PlannedDate': source.get('PlannedStartDateTime'),
            'CompletedDate': source.get('CompleteDateTime'), 'Status': source.get('Status'),
            'UpdatedDate': source.get('UpdateDateTime')}


def _stored(cursor, lms_ids, identity, *, lock=False):
    cursor.execute('''SELECT r.id,r.learner_id,r.aptem_review_id,r.review_type,
        r.review_data->%s,r.review_data->%s,
        coalesce(r.review_data ? %s,false),coalesce(r.review_data ? %s,false),
        r.status,r.completed_date
        FROM "Learner".reviews r WHERE r.learner_id=ANY(%s) ORDER BY r.id'''
        + (' FOR UPDATE' if lock else ''),
        [MEMBERSHIP_KEY, DOCUMENT_KEY, MEMBERSHIP_KEY, DOCUMENT_KEY, lms_ids])
    result = []
    for (pk, lms, review, kind, modern, document, has_modern, has_document,
         status, completed) in cursor.fetchall():
        if review is None:
            continue
        completed = _instant(completed)
        result.append({'id': pk, 'lms_learner_id': lms, 'aptem_learner_id': identity[str(lms)],
                       'aptem_review_id': _id(review), 'review_type': kind,
                       'modern_before': _json(modern), 'document_before': _json(document),
                       'modern_present': has_modern, 'document_present': has_document,
                       'status_before': status,
                       'completed_date_before': completed.isoformat() if completed else None})
    return result


def _verify_current(cursor, lms_ids, identity, staged):
    cursor.execute('''SELECT learner_id,aptem_review_id,review_data->%s,review_data->%s,status,completed_date
        FROM "Learner".reviews WHERE learner_id=ANY(%s)
        AND review_data->%s->>'current'='true' ''',
        [MEMBERSHIP_KEY, DOCUMENT_KEY, lms_ids, MEMBERSHIP_KEY])
    actual, stale_columns = {}, 0
    for lms, review, modern, document, status, completed in cursor.fetchall():
        key = identity[str(lms)], _id(review)
        if key in actual:
            raise ValueError('Duplicate identity in current membership readback')
        actual[key] = (_json(modern), _json(document))
        stale_columns += not _columns_match(status, completed, actual[key][0])
    expected = {(row['learner_id'], row['review_id']): (row['membership'], row['document']) for row in staged}
    if actual != expected:
        missing, extra = sorted(expected.keys() - actual.keys()), sorted(actual.keys() - expected.keys())
        raise ValueError(f'Exact Modern readback differs: {len(missing)} missing, {len(extra)} extra; '
                         'or membership/document metadata differs')
    if stale_columns:
        raise ValueError(f'Readback: {stale_columns} current reviews have status/completed_date '
                         'columns that differ from their Aptem membership')


def _write_changes(cursor, changes, learner, canonical, journal_path, journal):
    """Caller owns one learner transaction; journal is durable before its commit."""
    mutations = [change for change in changes if change['kind'] != 'unchanged']
    for change in mutations:
        if change['kind'] == 'insert':
            change['local_id'] = _insert_review(cursor, change['source'], canonical, change['membership'])
            journal['inserted_ids'].append(change['local_id'])
    payload = [{'id': change['local_id'], 'review_id': change['identity'][1],
                'membership': change['membership'], 'document': change['document'],
                'write_document': change['document'] is not None,
                'status': change.get('status'), 'completed_date': change.get('completed_date')}
               for change in mutations]
    if payload:
        # NULL status/completed_date = keep the column. Same statement as the JSON, so the
        # "Feedback".review_feedback_outbox trigger fires inside this learner transaction.
        cursor.execute('''UPDATE "Learner".reviews r SET review_data=
            coalesce(r.review_data,'{}'::jsonb) || jsonb_build_object(%s,p.membership)
            || CASE WHEN p.write_document THEN jsonb_build_object(%s,p.document) ELSE '{}'::jsonb END,
            status=coalesce(p.status,r.status),
            completed_date=coalesce(p.completed_date,r.completed_date)
            FROM jsonb_to_recordset(%s::jsonb) AS p(
                id bigint,review_id text,membership jsonb,document jsonb,write_document boolean,
                status text,completed_date timestamptz)
            WHERE r.id=p.id AND r.learner_id=%s AND r.aptem_review_id::text=p.review_id
            RETURNING r.id''', [MEMBERSHIP_KEY, DOCUMENT_KEY, json.dumps(payload), canonical])
        if {row[0] for row in cursor.fetchall()} != {change['local_id'] for change in mutations}:
            raise ValueError('Review identity changed during reconciliation')
    journal['after'] = payload
    write_verified(journal_path, journal)


def _insert_review(cursor, source, canonical, membership=None):
    """Same metadata layout as save_live_review, but NEVER upsert an unseen row.

    A concurrent importer inserting this identity must produce a uniqueness
    failure and roll back the learner, rather than silently overwrite a row
    that was absent from our durable before-image.
    """
    payload = _review_payload(source)
    status, completed = column_status(membership), column_completed(membership)
    review_id, learner = _id(payload['Id']), _id(payload['LearnerId'])
    data = {'aptem_learner_id': learner, 'aptem_review_id': review_id, 'live_odata': payload}
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    source_url = f'{settings.APTEM_BASE_URL.rstrip("/")}/pwa/learners/{learner}/reviews/review-content/{review_id}'
    cursor.execute('''INSERT INTO "Learner".reviews
        (learner_id,aptem_review_id,review_name,review_type,reviewer_name,
         planned_scheduled_date,completed_date,status,source_url,review_data,
         content_hash,extraction_status,last_error,learner_name,learner_email,
         first_seen_at,last_seen_at,created_at,updated_at)
        SELECT l.id,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'partial',%s,
            l.full_name,l.email,now(),now(),now(),now()
        FROM "Learner".learners l WHERE l.id=%s AND l.aptem_id::text=%s RETURNING id''',
        [review_id, (payload.get('Name') or '').strip(), payload['Type'].strip(),
         payload.get('OwnerName'), payload.get('PlannedDate'),
         completed.isoformat() if completed else payload.get('CompletedDate'),
         status or display_status(payload.get('Status')), source_url, encoded,
         hashlib.sha256(encoded.encode()).hexdigest(),
         'Aptem metadata only; review content has not been verified.', canonical, learner])
    row = cursor.fetchone()
    if not row:
        raise ValueError('Canonical learner identity changed before review insert')
    return row[0]


def apply_stage(stage_path, backup_path, *, apply=False, progress_path=None, progress=None,
                resume=False, lock_timeout_ms=5000, statement_timeout_ms=120000):
    """Apply in learner transactions; reruns re-read state and write only changes.

    Progress is committed-learner based. An interrupted COMMIT is explicitly
    unconfirmed and a rerun establishes truth by exact ID/metadata readback.
    """
    _, learners, stage_rows = load_stage(stage_path)
    successful = {learner: value for learner, value in learners.items() if value['status'] == 'ok'}
    expected = {str(_id(value['lms_learner_id'])): learner for learner, value in successful.items()}
    if len(expected) != len(successful):
        raise ValueError('Every successful Aptem learner needs a unique LMS learner ID')
    lms_ids = sorted(int(key) for key in expected)
    with connection.cursor() as cursor:
        cursor.execute('SELECT id,aptem_id FROM "Learner".learners WHERE id=ANY(%s)', [lms_ids])
        identity = {str(pk): _id(aptem) for pk, aptem in cursor.fetchall()}
        if identity != expected:
            dropped_lms = sorted(set(expected) - set(identity))
            mismatched = {k: (expected[k], identity[k]) for k in expected if k in identity and expected[k] != identity[k]}
            if mismatched:
                raise ValueError(f'LMS learner mapping differs (aptem_id mismatch): {list(mismatched.items())[:5]}')
            dropped_aptem = {expected[k] for k in dropped_lms}
            successful = {k: v for k, v in successful.items() if k not in dropped_aptem}
            expected = {k: v for k, v in expected.items() if k not in set(dropped_lms)}
            stage_rows = [r for r in stage_rows if r['learner_id'] not in dropped_aptem]
            lms_ids = sorted(int(key) for key in expected)
            import sys
            print(f'[apply_stage] Skipping {len(dropped_aptem)} learners deleted from Neon during capture: {sorted(dropped_aptem)[:10]}', file=sys.stderr)
        stored = _stored(cursor, lms_ids, identity)
    changes = plan_stage(learners, stage_rows, stored)
    report = {'stage_reviews': len(stage_rows), 'learners_total': len(successful),
              'updates': sum(c['kind'] == 'update' for c in changes),
              'inserts': sum(c['kind'] == 'insert' for c in changes),
              'historical_markers': sum(c['kind'] == 'historical' for c in changes),
              'unchanged': sum(c['kind'] == 'unchanged' for c in changes),
              'applied': False, 'deletions': 0, 'committed_learners': [],
              'committed_updates': 0, 'committed_inserts': 0, 'committed_historical': 0,
              'state': 'dry_run', 'exact_current_ids': False}
    if not apply:
        return report
    if lock_timeout_ms <= 0 or statement_timeout_ms <= 0:
        raise ValueError('Timeouts must be positive milliseconds')
    backup_path = Path(backup_path)
    progress_path = Path(progress_path) if progress_path else backup_path.with_suffix('.progress.json')
    journals = backup_path.with_suffix('.learners')
    digest = _checksum(Path(stage_path) / 'checksums.json')
    backup = {'schema_version': 2, 'created_at': datetime.now(timezone.utc).isoformat(),
              'stage': str(Path(stage_path).resolve()), 'stage_checksums_sha256': digest,
              'identity': identity, 'rows': stored, 'inserted_ids': [],
              'learner_journals': str(journals.resolve()),
              'recovery': 'Use each verified learner journal before/after and inserted_ids; '
                          'compare current metadata against after before restoring either key.'}
    if backup_path.exists():
        previous = load_verified(backup_path)
        if not resume or previous.get('stage_checksums_sha256') != digest or previous.get('identity') != identity:
            raise ValueError('Recovery file exists; use a fresh path or --resume for the exact same stage')
    else:
        if resume:
            raise ValueError('--resume requires an existing verified recovery file')
        write_verified(backup_path, backup)
    if load_verified(backup_path).get('stage_checksums_sha256') != digest:
        raise ValueError('Recovery file verification failed')
    grouped = defaultdict(list)
    for row in stage_rows:
        grouped[row['learner_id']].append(row)

    def emit(state, learner=None):
        report.update(state=state, active_learner=learner, updated_at=datetime.now(timezone.utc).isoformat(),
                      recovery_file=str(backup_path), progress_file=str(progress_path))
        write_verified(progress_path, report)
        if progress:
            progress({**report, 'committed_learners': list(report['committed_learners'])})

    emit('running')
    active = None
    try:
        for active in sorted(successful, key=int):
            canonical = int(successful[active]['lms_learner_id'])
            emit('running', active)
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('lock_timeout',%s,true),set_config('statement_timeout',%s,true)",
                               [f'{lock_timeout_ms}ms', f'{statement_timeout_ms}ms'])
                cursor.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                               ['aptem-modern-stage:' + active])
                if cursor.fetchone()[0] is not True:
                    raise ValueError('Another Modern reconciliation owns this learner')
                cursor.execute('SELECT aptem_id FROM "Learner".learners WHERE id=%s', [canonical])
                mapped = cursor.fetchone()
                if not mapped or _id(mapped[0]) != active:
                    raise ValueError('Canonical learner identity changed during reconciliation')
                current = _stored(cursor, [canonical], identity, lock=True)
                learner_changes = plan_stage({active: successful[active]}, grouped[active], current)
                mutations = [c for c in learner_changes if c['kind'] != 'unchanged']
                if mutations:
                    journals.mkdir(parents=True, exist_ok=True)
                    attempt = 1
                    while (journals / f'learner-{active}-{attempt}.json').exists():
                        attempt += 1
                    journal_path = journals / f'learner-{active}-{attempt}.json'
                    journal = {'schema_version': 2, 'learner_id': active, 'lms_learner_id': canonical,
                               'stage_checksums_sha256': digest, 'before': current, 'inserted_ids': [],
                               'planned_inserts': [c['identity'] for c in mutations if c['kind'] == 'insert'],
                               'created_at': datetime.now(timezone.utc).isoformat()}
                    write_verified(journal_path, journal)
                    _write_changes(cursor, learner_changes, active, canonical, journal_path, journal)
                _verify_current(cursor, [canonical], identity, grouped[active])
            # Advance only after transaction.atomic has returned (commit done).
            report['committed_learners'].append(active)
            report['committed_updates'] += sum(c['kind'] == 'update' for c in mutations)
            report['committed_inserts'] += sum(c['kind'] == 'insert' for c in mutations)
            report['committed_historical'] += sum(c['kind'] == 'historical' for c in mutations)
            emit('running', active)
        with connection.cursor() as cursor:
            _verify_current(cursor, lms_ids, identity, stage_rows)
        report.update(applied=True, exact_current_ids=True)
        emit('complete')
        return report
    except (Exception, KeyboardInterrupt) as exc:
        report['failed_learner'] = active
        report['error_type'] = type(exc).__name__
        # A connection loss during COMMIT cannot be classified as rolled back.
        report['failed_learner_commit'] = 'unconfirmed; exact readback required on resume'
        emit('failed', active)
        raise StageApplyError(f'Modern apply stopped at learner {active}; '
                              f'{len(report["committed_learners"])} learners committed. '
                              f'{type(exc).__name__}: {exc}. Progress: {progress_path}', report) from exc
