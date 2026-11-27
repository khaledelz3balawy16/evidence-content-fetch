import json
from django.core.management.base import BaseCommand, CommandError
from aptem.services.modern_review_recovery import restore_journal


class Command(BaseCommand):
    help = 'Dry-run or restore one Modern metadata learner journal; never delete review content.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--journal', required=True)
        parser.add_argument('--backup')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        try:
            result = restore_journal(options['journal'], apply=options['apply'], backup_path=options['backup'])
        except (ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result))
