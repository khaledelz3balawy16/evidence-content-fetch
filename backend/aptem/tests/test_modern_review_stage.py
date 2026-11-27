import hashlib
import json
from pathlib import Path
import sqlite3
from copy import deepcopy
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

from aptem.services import modern_review_stage as stage


def write(path, value):
    Path(path).write_text(json.dumps(value), encoding='utf-8')


def membership(review='100', learner='10'):
    return {'schema_version': 1, 'source': 'modern_planned_finished',
            'instance': stage.INSTANCE, 'aptem_learner_id': learner,
            'aptem_review_id': review, 'run_id': 'run', 'checked_at': '2026-10-24T12:00:00+00:00',
            'verified': True, 'both_tabs_complete': True, 'current': True,
            'listing_program_id': '20', 'source_program_id': None, 'tab': 'planned',
            'source_status': 'Awaiting Signature', 'planned_at': None, 'completed_at': None}


def staged_row(review='100', learner='10'):
    identity = {'aptem_learner_id': learner, 'aptem_review_id': review}
    return {'learner_id': learner, 'review_id': review, 'membership': membership(review, learner),
            'document': {'selection': {**identity, 'state': 'missing'}, 'evidence': identity},
            'source': {'Id': int(review), 'UserId': int(learner), 'TypeName': 'Progress Review',
                       'Status': 'Awaiting Signature', 'PlannedStartDateTime': None, 'CompleteDateTime': None}}


def stored_row(review='100', learner='10', lms=1):
    return {'id': int(review), 'lms_learner_id': lms, 'aptem_learner_id': learner,
            'aptem_review_id': review, 'review_type': 'Progress Review',
            'modern_before': None, 'document_before': None,
            'modern_present': False, 'document_present': False,
            'status_before': 'Awaiting Signature', 'completed_date_before': None}


class FakeDatabase:
    """Small SQL boundary fake: transaction commits and rollbacks are observable."""
    def __init__(self, rows, mapping):
        self.rows, self.mapping = deepcopy(rows), mapping
        self.result, self.updates, self.fail_for_learner = [], 0, None
        self.commits = 0

    @contextmanager
    def cursor(self):
        yield self

    @contextmanager
    def atomic(self):
        before = deepcopy(self.rows)
        try:
            yield
        except BaseException:
            self.rows = before
            raise
        else:
            self.commits += 1

    def execute(self, sql, params):
        if 'SELECT id,aptem_id' in sql:
            self.result = [(int(k), v) for k, v in self.mapping.items()]
        elif 'SELECT aptem_id' in sql:
            self.result = [(self.mapping[str(params[0])],)]
        elif 'set_config' in sql:
            self.result = [(None, None)]
        elif 'pg_try_advisory_xact_lock' in sql:
            self.result = [(True,)]
        elif 'SELECT r.id,r.learner_id' in sql:
            self.result = [(r['id'], r['lms_learner_id'], r['aptem_review_id'], r['review_type'],
                            r['modern_before'], r['document_before'],
                            r['modern_present'], r['document_present'],
                            r['status_before'], r['completed_date_before'])
                           for r in self.rows if r['lms_learner_id'] in params[-1]]
        elif sql.startswith('UPDATE'):
            self.updates += 1
            payload = json.loads(params[2])
            self.result = []
            for change in payload:
                row = next(r for r in self.rows if r['id'] == change['id'])
                row['modern_before'], row['modern_present'] = change['membership'], True
                if change['write_document']:
                    row['document_before'], row['document_present'] = change['document'], True
                row['status_before'] = change['status'] or row['status_before']
                row['completed_date_before'] = change['completed_date'] or row['completed_date_before']
                self.result.append((row['id'],))
        elif 'SELECT learner_id,aptem_review_id' in sql:
            if self.fail_for_learner in params[2]:
                raise ValueError('Injected readback failure after metadata update')
            self.result = [(r['lms_learner_id'], r['aptem_review_id'], r['modern_before'], r['document_before'],
                            r['status_before'], r['completed_date_before'])
                           for r in self.rows if r['lms_learner_id'] in params[2]
                           and (r['modern_before'] or {}).get('current') is True]
        else:
            raise AssertionError('Unexpected SQL: ' + sql)

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0] if self.result else None


class ModernReviewStageTests(SimpleTestCase):
    def test_noop_and_partial_document_repair(self):
        row = staged_row()
        stored = stored_row()
        stored['modern_before'] = deepcopy(row['membership'])
        learners = {'10': {'status': 'ok'}}
        self.assertEqual(stage.plan_stage(learners, [row], [stored])[0]['kind'], 'update')
        stored['document_before'] = deepcopy(row['document'])
        self.assertEqual(stage.plan_stage(learners, [row], [stored])[0]['kind'], 'unchanged')

    def test_empty_verified_inventory_archives_and_enrollment_is_excluded(self):
        learners = {'10': {'status': 'ok', 'membership': membership()}}
        enrollment = {**stored_row('99'), 'review_type': 'Enrollment Review'}
        plan = stage.plan_stage(learners, [], [stored_row(), enrollment])
        self.assertEqual([(r['kind'], r['identity']) for r in plan], [('historical', ('10', '100'))])

    def test_listing_program_does_not_replace_source_program(self):
        row = staged_row()
        row['membership']['source_program_id'] = '30'
        old = stored_row()
        old['modern_before'] = {**row['membership'], 'listing_program_id': '99'}
        self.assertEqual(stage.plan_stage({'10': {'status': 'ok'}}, [row], [old])[0]['kind'], 'update')
        old['modern_before']['source_program_id'] = '31'
        with self.assertRaisesRegex(ValueError, 'source program'):
            stage.plan_stage({'10': {'status': 'ok'}}, [row], [old])

    def test_stale_snapshot_or_attempt_is_rejected(self):
        row, old = staged_row(), stored_row()
        old['modern_before'] = {**row['membership'], 'checked_at': '2026-10-24T13:00:00Z'}
        with self.assertRaisesRegex(ValueError, 'older'):
            stage.plan_stage({'10': {'status': 'ok'}}, [row], [old])
        old['modern_before'] = {**row['membership'], 'last_attempt': {'checked_at': '2026-10-24T13:00:00Z'}}
        with self.assertRaisesRegex(ValueError, 'older'):
            stage.plan_stage({'10': {'status': 'ok'}}, [row], [old])

    def test_equal_count_wrong_id_fails_exact_readback(self):
        db = FakeDatabase([{**stored_row('99'), 'modern_before': membership('99')}], {'1': '10'})
        with self.assertRaisesRegex(ValueError, '1 missing, 1 extra'):
            stage._verify_current(db, [1], {'1': '10'}, [staged_row()])

    def test_unmatched_identity_or_duplicate_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            stage.plan_stage({'10': {'status': 'ok'}}, [staged_row(), staged_row()], [])
        with self.assertRaisesRegex(ValueError, 'unsuccessful'):
            stage.plan_stage({'10': {'status': 'no_current_program'}}, [staged_row()], [])

    def test_payload_uses_modern_type_name(self):
        self.assertEqual(stage._review_payload(staged_row()['source'])['Type'], 'Progress Review')

    def test_per_learner_commit_and_failure_rollback_with_checkpoint(self):
        rows = [staged_row(), staged_row('200', '11')]
        initial = [stored_row(), stored_row('200', '11', 2)]
        learners = {'10': {'status': 'ok', 'lms_learner_id': '1'},
                    '11': {'status': 'ok', 'lms_learner_id': '2'}}
        db = FakeDatabase(initial, {'1': '10', '2': '11'})
        db.fail_for_learner = 2
        with TemporaryDirectory() as folder, patch.object(stage, 'load_stage', return_value=({}, learners, rows)), \
                patch.object(stage, 'connection', db), patch.object(stage.transaction, 'atomic', db.atomic):
            root = Path(folder)
            write(root / 'checksums.json', {})
            backup = root / 'backup.json'
            with self.assertRaises(stage.StageApplyError) as failure:
                stage.apply_stage(root, backup, apply=True)
            self.assertEqual(db.commits, 1)
            self.assertEqual(db.rows[0]['modern_before'], rows[0]['membership'])
            self.assertEqual(db.rows[0]['document_before'], rows[0]['document'])
            self.assertEqual(db.rows[1], initial[1])
            self.assertEqual(failure.exception.report['committed_learners'], ['10'])
            self.assertEqual(stage.load_verified(backup.with_suffix('.progress.json'))['state'], 'failed')
            journal = stage.load_verified(backup.with_suffix('.learners') / 'learner-10-1.json')
            self.assertEqual(journal['before'], [initial[0]])
            self.assertEqual(journal['after'][0]['document'], rows[0]['document'])
            # Retry verifies the already committed learner, and writes only the failed learner.
            db.fail_for_learner = None
            result = stage.apply_stage(root, backup, apply=True, resume=True)
            self.assertTrue(result['exact_current_ids'])
            self.assertEqual(result['committed_updates'], 1)
            self.assertEqual(db.updates, 3)  # first committed, failed/rolled back, resumed
            result = stage.apply_stage(root, backup, apply=True, resume=True)
            self.assertEqual(result['committed_updates'], 0)
            self.assertEqual(db.updates, 3)

    def test_plan_marks_only_successful_learner_history_and_never_deletes(self):
        learners = {'10': {'status': 'ok', 'lms_learner_id': '1'},
                    '11': {'status': 'no_current_program', 'lms_learner_id': '2'}}
        membership = {'schema_version': 1, 'source': 'modern_planned_finished',
                      'instance': 'kentbusinesscollege.aptem.co.uk', 'aptem_learner_id': '10',
                      'aptem_review_id': '100', 'run_id': 'run', 'checked_at': '2026-10-24T12:00:00+00:00',
                      'verified': True, 'both_tabs_complete': True, 'current': True,
                      'listing_program_id': '20', 'tab': 'planned'}
        rows = [{'learner_id': '10', 'review_id': '100', 'membership': membership,
                 'document': {}, 'source': {'Id': 100}},]
        stored = [{'id': 1, 'aptem_learner_id': '10', 'aptem_review_id': '100'},
                  {'id': 2, 'aptem_learner_id': '10', 'aptem_review_id': '99'},
                  {'id': 3, 'aptem_learner_id': '11', 'aptem_review_id': '98'}]
        changes = stage.plan_stage(learners, rows, stored)
        self.assertEqual([(change['kind'], change['identity']) for change in changes],
                         [('update', ('10', '100')), ('historical', ('10', '99'))])
        self.assertFalse(changes[1]['membership']['current'])
        self.assertTrue(all(change['kind'] != 'delete' for change in changes))

    def test_stage_checksum_and_incomplete_summary_are_rejected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            db = sqlite3.connect(root / 'reviews.sqlite3')
            db.executescript('CREATE TABLE learners (aptem_learner_id,lms_learner_id,status,checked_at);'
                             'CREATE TABLE current_reviews (aptem_learner_id,aptem_review_id,existing_local_id,review_json,metadata_json);')
            db.close()
            summary = {'mode': 'live_capture_local_staging', 'exact_current_ids': True,
                       'sqlite_integrity': 'ok', 'validation_errors': [],
                       'shared_database_writes': 0, 'deletions': 0}
            write(root / 'stage-summary.json', summary)
            checksums = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                         for name in ('reviews.sqlite3', 'stage-summary.json')}
            write(root / 'checksums.json', checksums)
            loaded = stage.load_stage(root)
            self.assertEqual(loaded[0]['mode'], summary['mode'])
            (root / 'reviews.sqlite3').write_bytes(b'changed')
            with self.assertRaises(ValueError): stage.load_stage(root)


class ReviewColumnSyncTests(SimpleTestCase):
    """status/completed_date follow aptem_modern; NULL/absent keys keep the column."""

    def test_status_normalization(self):
        self.assertEqual(stage.column_status({'source_status': 'Complete'}), 'Completed')
        self.assertEqual(stage.column_status({'source_status': 'COMPLETED '}), 'Completed')
        self.assertEqual(stage.column_status({'source_status': 'AwaitingSignature'}), 'Awaiting Signature')
        self.assertEqual(stage.column_status({'source_status': 'NotScheduled'}), 'Not Scheduled')
        self.assertEqual(stage.column_status({'source_status': 'Scheduled'}), 'Scheduled')
        self.assertIsNone(stage.column_status({}))
        self.assertIsNone(stage.column_status({'source_status': ''}))

    def test_wall_clock_completed_at_is_london_time(self):
        bst = {'completed_at': '2026-09-07T15:58:47', 'date_semantics': 'source_wall_clock'}
        gmt = {'completed_at': '2026-02-13T11:50:37', 'date_semantics': 'source_wall_clock'}
        self.assertEqual(stage.column_completed(bst).isoformat(), '2026-09-07T14:58:47+00:00')
        self.assertEqual(stage.column_completed(gmt).isoformat(), '2026-02-13T11:50:37+00:00')
        self.assertIsNone(stage.column_completed({'completed_at': None}))
        with self.assertRaisesRegex(ValueError, 'offset'):
            stage.column_completed({'completed_at': '2026-02-13T11:50:37'})

    def test_writes_only_real_changes(self):
        member = {**membership(), 'source_status': 'Completed', 'completed_at': '2026-09-07T15:58:47',
                  'date_semantics': 'source_wall_clock'}
        stored = {'status_before': 'Completed', 'completed_date_before': '2026-09-07T14:58:47.594710+00:00'}
        self.assertEqual(stage.column_writes(stored, member), (None, None))
        stored = {'status_before': 'Scheduled', 'completed_date_before': None}
        self.assertEqual(stage.column_writes(stored, member), ('Completed', '2026-09-07T14:58:47+00:00'))
        formatting = {**membership(), 'source_status': 'Awaiting Signature'}
        self.assertEqual(stage.column_writes({'status_before': 'AwaitingSignature'}, formatting), (None, None))
        # Absent/empty keys keep the old column values.
        absent = {k: v for k, v in membership().items() if k not in ('source_status', 'completed_at')}
        self.assertEqual(stage.column_writes({'status_before': 'Completed',
                                              'completed_date_before': '2026-01-01T00:00:00+00:00'}, absent),
                         (None, None))
        self.assertEqual(stage.column_writes({'status_before': 'Completed'}, membership())[1], None)

    def test_stale_column_alone_is_an_update(self):
        row, stored = staged_row(), stored_row()
        stored['modern_before'], stored['document_before'] = deepcopy(row['membership']), deepcopy(row['document'])
        self.assertEqual(stage.plan_stage({'10': {'status': 'ok'}}, [row], [stored])[0]['kind'], 'unchanged')
        stored['status_before'] = 'Scheduled'
        change = stage.plan_stage({'10': {'status': 'ok'}}, [row], [stored])[0]
        self.assertEqual((change['kind'], change['status']), ('update', 'Awaiting Signature'))

    def test_historical_marker_keeps_columns(self):
        learners = {'10': {'status': 'ok', 'membership': membership()}}
        change = stage.plan_stage(learners, [], [{**stored_row(), 'status_before': 'Scheduled'}])[0]
        self.assertEqual((change['kind'], change['status'], change['completed_date']), ('historical', None, None))

    def test_apply_writes_columns_with_json_and_readback_checks_them(self):
        row = staged_row()
        row['membership'].update(source_status='Completed', completed_at='2026-11-25T10:38:57',
                                 date_semantics='source_wall_clock', tab='finished')
        initial = [{**stored_row(), 'status_before': 'Not Scheduled'}]
        learners = {'10': {'status': 'ok', 'lms_learner_id': '1'}}
        db = FakeDatabase(initial, {'1': '10'})
        with TemporaryDirectory() as folder, patch.object(stage, 'load_stage', return_value=({}, learners, [row])), \
                patch.object(stage, 'connection', db), patch.object(stage.transaction, 'atomic', db.atomic):
            root = Path(folder)
            write(root / 'checksums.json', {})
            result = stage.apply_stage(root, root / 'backup.json', apply=True)
            self.assertTrue(result['exact_current_ids'])
            self.assertEqual(db.rows[0]['status_before'], 'Completed')
            self.assertEqual(db.rows[0]['completed_date_before'], '2026-11-25T10:38:57+00:00')
            journal = stage.load_verified(root / 'backup.learners' / 'learner-10-1.json')
            self.assertEqual(journal['before'][0]['status_before'], 'Not Scheduled')
            self.assertEqual(journal['after'][0]['status'], 'Completed')
        db.rows[0]['status_before'] = 'Scheduled'
        with self.assertRaisesRegex(ValueError, 'columns that differ'):
            stage._verify_current(db, [1], {'1': '10'}, [row])
