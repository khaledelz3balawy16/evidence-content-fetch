"""Read-only exact membership and evidence comparison after a Modern apply."""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from aptem.services.modern_review_stage import load_stage
from aptem.services.review_signatures import write_verified


class Command(BaseCommand):
    help = 'Compare exact stored Modern identities and evidence with a verified stage; read-only DB access.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--stage', required=True)
        parser.add_argument('--report', type=Path, required=True)

    def handle(self, *args, **options):
        _, learners, rows = load_stage(options['stage'])
        ids = {int(v['lms_learner_id']): k for k, v in learners.items() if v['status'] == 'ok'}
        expected = {(r['learner_id'], r['review_id']): r for r in rows}
        current, stored, historical, states = {}, {}, [], Counter()
        duplicates = []
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cursor.execute('''SELECT learner_id,aptem_review_id,review_data->'aptem_modern',
                review_data->'aptem_classic_document',id FROM "Learner".reviews
                WHERE learner_id=ANY(%s)''', [list(ids)])
            for canonical, rid, member, document, pk in cursor.fetchall():
                if rid is None:
                    continue
                key = ids[canonical], str(rid)
                member = json.loads(member) if isinstance(member, str) else member
                document = json.loads(document) if isinstance(document, str) else document
                if key in stored:
                    duplicates.append(key)
                stored[key] = pk
                if member and member.get('current') is True:
                    current[key] = (member, document)
                    states[(document or {}).get('selection', {}).get('state', 'unknown')] += 1
                elif member and member.get('current') is False:
                    historical.append({'learner_id': key[0], 'review_id': key[1], 'lms_review_id': pk})
        missing = sorted(expected.keys() - current.keys())
        extra = sorted(current.keys() - expected.keys())
        mismatch = [key for key in expected.keys() & current.keys()
                    if current[key] != (expected[key]['membership'], expected[key]['document'])]
        report = {'checked_at': datetime.now(timezone.utc).isoformat(),
                  'scope': 'successful complete Modern inventories only',
                  'learners_verified': len(ids), 'source_states': dict(Counter(v['status'] for v in learners.values())),
                  'expected_current': len(expected), 'stored_current': len(current),
                  'missing': missing, 'extra': extra, 'metadata_mismatches': mismatch, 'duplicates': duplicates,
                  'exact_current_ids': not (missing or extra or duplicates),
                  'exact_metadata': not (missing or extra or mismatch or duplicates),
                  'historical_count': len(historical), 'historical': historical,
                  'document_states': dict(states), 'database_writes': 0, 'deletions': 0}
        write_verified(options['report'], report)
        self.stdout.write(json.dumps({k: v for k, v in report.items() if k != 'historical'}))
        if not report['exact_metadata']:
            raise CommandError('Modern readback differs; see the comparison report')
