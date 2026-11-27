"""Read the live Modern tabs and build a local, checksum-verified review stage."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import time
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from aptem.services.aptem_local_stage import stage
from aptem.services.aptem_review_contract import identity


def write_json(path, value):
    body = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(body)
    for attempt in range(15):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 14:
                raise
            time.sleep(min(0.5, 0.03 * (attempt + 1)))
    path.with_suffix(path.suffix + '.sha256').write_text(hashlib.sha256(body).hexdigest(), encoding='ascii')


def export_inventory(selected=None):
    """One read-only DB snapshot, with numeric IDs and source labels only."""
    if connection.vendor != 'postgresql':
        raise ValueError('Canonical learner export requires the configured Postgres database')
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cursor.execute('SET LOCAL statement_timeout = 60000')
            cursor.execute('''SELECT id,aptem_id,to_jsonb(l)->>'programme_id',to_jsonb(l)->>'cohort_id'
                FROM "Learner".learners l WHERE aptem_id IS NOT NULL ORDER BY id''')
            candidates = []
            seen = set()
            for pk, aid, programme, cohort in cursor.fetchall():
                aid = identity(aid)
                if selected and aid not in selected:
                    continue
                if aid in seen:
                    raise ValueError('Duplicate canonical Aptem learner identity: ' + aid)
                seen.add(aid)
                candidates.append({'lms_learner_id': str(pk), 'aptem_id': aid,
                                   'lms_programme_id': programme, 'cohort_id': cohort})
            if not candidates or (selected and seen != selected):
                raise ValueError('One or more selected learners have no canonical mapping')
            by_lms = {int(c['lms_learner_id']): {'aptem_id': c['aptem_id'], 'reviews': []}
                      for c in candidates}
            cursor.execute('''SELECT id,learner_id,aptem_review_id,status,review_type
                FROM "Learner".reviews WHERE learner_id=ANY(%s) AND aptem_review_id IS NOT NULL
                AND lower(coalesce(review_type,'')) NOT LIKE '%%enrol%%' ORDER BY id''', [list(by_lms)])
            for pk, learner, review, status, review_type in cursor.fetchall():
                by_lms[learner]['reviews'].append({'lms_review_id': str(pk), 'review_id': str(review),
                                                 'status': status, 'review_type': review_type})
    return candidates, list(by_lms.values())


class Command(BaseCommand):
    help = 'Capture only Aptem Modern Planned/Finished using local workers, then stage locally; no DB writes.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--output', type=Path)
        parser.add_argument('--workers', type=int, choices=(1, 2, 3, 4), default=3)
        parser.add_argument('--aptem-learner-ids', default='')
        parser.add_argument('--verify-ui', action='store_true')
        parser.add_argument('--ui-learners', default='106,16098,4907')
        parser.add_argument('--resume', action='store_true')

    def handle(self, *args, **options):
        root = Path(settings.BASE_DIR).parent.resolve()
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        output = (options['output'] or root / 'debug' / 'modern-reviews' / (stamp + '-' + uuid.uuid4().hex[:8])).resolve()
        if output.exists() and not options['resume']:
            raise CommandError('Use a fresh output directory; previous capture and recovery evidence is immutable')
        if options['resume'] and (not options['output'] or not (output / 'candidates.json').exists()):
            raise CommandError('--resume requires an existing --output capture directory')
        if (output / 'stage').exists():
            raise CommandError('This capture already has a stage; use it or start a new run')
        output.mkdir(parents=True, exist_ok=True)
        status = {'started_at': datetime.now(timezone.utc).isoformat(), 'pid': os.getpid(),
                  'phase': 'export_inventory', 'state': 'running', 'workers': options['workers'],
                  'output': str(output), 'database_writes': 0, 'deletions': 0}

        def update(**values):
            status.update(values, updated_at=datetime.now(timezone.utc).isoformat())
            write_json(output / 'run-status.json', status)
            with (output / 'phase-history.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(status) + '\n')

        update()
        try:
            selected = {identity(v.strip()) for v in options['aptem_learner_ids'].split(',') if v.strip()}
            if options['resume']:
                for name in ('candidates.json', 'stored.json'):
                    body = (output / name).read_bytes()
                    if hashlib.sha256(body).hexdigest() != (output / (name + '.sha256')).read_text():
                        raise ValueError('Resume inventory checksum mismatch')
                candidates = json.loads((output / 'candidates.json').read_text(encoding='utf-8'))
                if selected and selected != {str(c['aptem_id']) for c in candidates}:
                    raise ValueError('Resume learner selection differs from the saved inventory')
            else:
                candidates, stored = export_inventory(selected)
                write_json(output / 'candidates.json', candidates)
                write_json(output / 'stored.json', stored)
            connection.close()
            # Project-local Playwright driver: node.exe on Windows, node on Linux (VPS).
            # APTEM_NODE_BINARY overrides it if the runtime lives elsewhere.
            node = Path(os.environ.get('APTEM_NODE_BINARY') or (
                root / '.playwright-runtime' / 'playwright' / 'driver' / ('node.exe' if os.name == 'nt' else 'node')))
            collector = root / 'scripts' / 'capture-aptem-modern.cjs'
            if not node.exists() or not collector.exists():
                raise ValueError('Project-local Playwright runtime or collector is missing')
            command = [str(node), str(collector), '--credentials', str(Path(settings.BASE_DIR) / '.env'),
                       '--candidates', str(output / 'candidates.json'), '--output', str(output / 'capture'),
                       '--workers', str(options['workers'])]
            if options['verify_ui']:
                command += ['--verify-ui', 'true', '--ui-learners', options['ui_learners']]
            update(phase='capture', expected_learners=len(candidates))
            with (output / 'capture.log').open('a' if options['resume'] else 'w', encoding='utf-8') as log:
                child = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
                update(collector_pid=child.pid)
                self.stdout.write(json.dumps({'started': True, 'pid': os.getpid(), 'collector_pid': child.pid,
                                              'workers': options['workers'], 'output': str(output)}))
                self.stdout.flush()
                try:
                    for line in child.stdout:
                        log.write(line)
                        log.flush()
                        self.stdout.write(line.rstrip())
                        self.stdout.flush()
                        try:
                            progress = json.loads(line)
                        except ValueError:
                            progress = {'message': line.strip()[:200]}
                        update(progress=progress)
                    code = child.wait()
                except BaseException:
                    child.terminate()
                    child.wait(timeout=15)
                    raise
            if code:
                raise ValueError('Aptem collector failed; see project-local capture.log')
            update(phase='stage')
            result = stage(output / 'capture', output / 'stored.json', output / 'stage')
            if not result['exact_current_ids'] or result['validation_errors'] or result['sqlite_integrity'] != 'ok':
                raise ValueError('Stage validation failed; shared database remains unchanged')
            update(phase='complete', state='complete', result=result, stage=str(output / 'stage'))
            self.stdout.write(json.dumps({'output': str(output), 'stage': str(output / 'stage'), **result}))
        except BaseException as exc:
            update(state='failed', error=type(exc).__name__ + ': ' + str(exc).split('\n')[0][:200])
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise CommandError(str(exc)) from exc
