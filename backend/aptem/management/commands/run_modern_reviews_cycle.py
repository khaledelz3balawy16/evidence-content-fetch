"""Explicit Modern-only cycle, isolated from the older all-OData scheduler."""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from aptem.management.commands.capture_modern_reviews import write_json


class Command(BaseCommand):
    help = 'Capture Modern Planned/Finished, validate, and optionally apply recoverable metadata changes.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--output', type=Path)
        parser.add_argument('--workers', type=int, choices=(1, 2, 3, 4), default=3)
        parser.add_argument('--aptem-learner-ids', default='')
        parser.add_argument('--verify-ui', action='store_true')

    def handle(self, *args, **options):
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        output = (options['output'] or settings.BASE_DIR.parent / 'debug' / 'modern-reviews' /
                  (stamp + '-' + uuid.uuid4().hex[:8])).resolve()
        if output.exists():
            raise CommandError('Use a fresh run directory')
        try:
            call_command('capture_modern_reviews', output=output, workers=options['workers'],
                         aptem_learner_ids=options['aptem_learner_ids'], verify_ui=options['verify_ui'],
                         stdout=self.stdout, stderr=self.stderr)
            write_json(output / 'cycle-status.json', {'state': 'applying' if options['apply'] else 'planning',
                                                      'output': str(output)})
            call_command('apply_modern_review_stage', stage=str(output / 'stage'),
                         backup=str(output / 'recovery' / 'before-apply.json'),
                         progress=str(output / 'apply-progress.json'), apply=options['apply'],
                         stdout=self.stdout, stderr=self.stderr)
            write_json(output / 'cycle-status.json', {'state': 'complete' if options['apply'] else 'dry_run',
                                                      'output': str(output), 'deletions': 0})
        except Exception as exc:
            if output.exists():
                write_json(output / 'cycle-status.json', {'state': 'failed', 'error_type': type(exc).__name__,
                                                          'output': str(output)})
            raise
        self.stdout.write(json.dumps({'state': 'complete' if options['apply'] else 'dry_run',
                                      'output': str(output), 'applied': options['apply']}))
