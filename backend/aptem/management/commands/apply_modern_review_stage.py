import json

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from aptem.services.modern_review_stage import apply_stage
from aptem.services.review_api_sync import ReviewSyncError


class Command(BaseCommand):
    help = 'Apply a checksum-verified Aptem Modern local stage; no review deletion is possible.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--stage', required=True)
        parser.add_argument('--backup', required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--resume', action='store_true')
        parser.add_argument('--progress', help='JSON progress path; defaults beside the recovery file')
        parser.add_argument('--lock-timeout-ms', type=int, default=5000)
        parser.add_argument('--statement-timeout-ms', type=int, default=120000)

    def handle(self, *args, **options):
        def show_progress(value):
            self.stdout.write(json.dumps({key: value.get(key) for key in (
                'state', 'active_learner', 'learners_total', 'committed_updates',
                'committed_inserts', 'committed_historical', 'updated_at')}
                | {'learners_done': len(value['committed_learners'])}))
            self.stdout.flush()

        try:
            report = apply_stage(options['stage'], options['backup'], apply=options['apply'],
                                 resume=options['resume'], progress_path=options['progress'],
                                 progress=show_progress, lock_timeout_ms=options['lock_timeout_ms'],
                                 statement_timeout_ms=options['statement_timeout_ms'])
        except (ValueError, OSError, ReviewSyncError, DatabaseError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(report))
