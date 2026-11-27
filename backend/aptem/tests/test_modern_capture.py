"""Offline integration tests for Modern capture completeness and safe staging."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from aptem.services import aptem_local_stage as local
from aptem.services.aptem_review_contract import ContractError


def bucket(items):
    return {'complete': True, 'items': items, 'total': len(items),
            'pages': [{'page': 1, 'http_status': 200, 'count': len(items)}]}


def capture(learner='106', *, status='ok', empty=False):
    checked = datetime.now(timezone.utc).isoformat()
    rows = [] if empty else [{'Id': 23478, 'UserId': int(learner), 'ProgramId': 20260,
                             'TypeName': 'Progress Review', 'Name': 'Progress Review 1',
                             'Status': 'In Progress', 'PlannedStartDateTime': '2026-10-20T00:00:00',
                             'CompleteDateTime': None}]
    return {'aptem_id': learner, 'lms_learner_id': '1117', 'run_id': 'test-run',
            'status': status, 'aptem_program_id': '20260', 'checked_at': checked,
            'membership_checked_at': checked, 'planned': [{'id': row['Id'], 'tab': 'planned',
            'type': row['TypeName'], 'status': row['Status']} for row in rows], 'finished': [],
            'modern_buckets': {'planned': bucket(rows), 'finished': bucket([])},
            'groups': [], 'groups_by_program': [], 'document_errors': [], 'documents': {}}


def write_verified(path, value):
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    path.with_suffix('.json.sha256').write_text(hashlib.sha256(raw).hexdigest())


def prepare(root, profiles, expected=None, state='complete'):
    folder = root / 'capture'
    (folder / 'learners').mkdir(parents=True)
    write_verified(folder / 'manifest.json', {'state': state, 'run_id': 'test-run',
        'expected_learner_ids': expected or [p['aptem_id'] for p in profiles]})
    for profile in profiles:
        write_verified(folder / 'learners' / (profile['aptem_id'] + '.json'), profile)
    return folder


class CaptureCompletenessTests(unittest.TestCase):
    def test_refuses_finished_manifest_with_missing_learner(self):
        with TemporaryDirectory() as temp:
            folder = prepare(Path(temp), [capture()], expected=['106', '16098'])
            with self.assertRaisesRegex(ContractError, 'expected learner'):
                local.load_capture(folder)

    def test_refuses_foreign_run_even_with_valid_file_checksum(self):
        with TemporaryDirectory() as temp:
            profile = capture()
            profile['run_id'] = 'other-run'
            folder = prepare(Path(temp), [profile])
            with self.assertRaisesRegex(ContractError, 'another learner or run'):
                local.load_capture(folder)

    def test_refuses_running_capture(self):
        with TemporaryDirectory() as temp:
            folder = prepare(Path(temp), [capture()], state='running')
            with self.assertRaisesRegex(ContractError, 'did not finish'):
                local.load_capture(folder)

    def test_stages_exact_modern_ids_and_retains_unknown_learners(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            profiles = [capture(), capture('3270', status='no_current_program', empty=True),
                        capture('4907', status='error', empty=True)]
            folder = prepare(root, profiles)
            stored = root / 'stored.json'
            stored.write_text(json.dumps([{'aptem_id': p['aptem_id'], 'reviews': [
                {'lms_review_id': '50', 'review_id': '1', 'status': 'Completed'}]} for p in profiles]))
            result = local.stage(folder, stored, root / 'stage')
            self.assertTrue(result['exact_current_ids'])
            self.assertEqual(result['staged_current_reviews'], 1)
            self.assertEqual(result['shared_database_writes'], 0)
            db = sqlite3.connect(root / 'stage' / 'reviews.sqlite3')
            try:
                row = db.execute('SELECT aptem_learner_id,aptem_review_id,modern_tab,planned_at FROM current_reviews').fetchone()
            finally:
                db.close()
            self.assertEqual(row, ('106', '23478', 'planned', '2026-10-20T00:00:00'))
            report = json.loads((root / 'stage' / 'report.json').read_text())
            for learner in report['learners']:
                if learner['source_state'] != 'ok':
                    self.assertEqual(learner['outside'], [])

    def test_successful_empty_lists_preserve_proof_for_historical_membership(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            profile = capture(empty=True)
            folder = prepare(root, [profile])
            stored = root / 'stored.json'
            stored.write_text(json.dumps([{'aptem_id': '106', 'reviews': [
                {'lms_review_id': '50', 'review_id': '1', 'status': 'Completed'}]}]))
            result = local.stage(folder, stored, root / 'stage')
            report = json.loads((root / 'stage' / 'report.json').read_text())
            saved = json.loads((root / 'stage' / 'live.jsonl').read_text())
            self.assertEqual(result['staged_current_reviews'], 0)
            self.assertEqual(report['counts']['outside_candidates'], 1)
            self.assertEqual(saved['aptem_program_id'], '20260')
            self.assertTrue(saved['modern_buckets']['finished']['complete'])


if __name__ == '__main__':
    unittest.main()
