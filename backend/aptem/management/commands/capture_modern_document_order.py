"""Read server-sorted Classic document IDs for unresolved exact review groups."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from aptem.services.modern_review_stage import load_stage
from aptem.services.review_api_sync import login_session
from aptem.services.review_signatures import load_verified, write_verified


class Command(BaseCommand):
    help = 'Read-only server ordering evidence for ambiguous Classic document creation dates.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--stage', required=True)
        parser.add_argument('--output', type=Path, required=True)

    def handle(self, *args, **options):
        _, _, rows = load_stage(options['stage'])
        selected = [r for r in rows if 'Latest document timestamps' in r['document']['selection'].get('reason', '')]
        output = options['output']
        if output.exists():
            raise CommandError('Use a new output path')
        session = login_session()
        session.headers.update({'User-Agent': 'Mozilla/5.0', 'X-Requested-With': 'XMLHttpRequest',
                                'Accept': 'application/json'})
        report = {'source': 'classic_server_order', 'state': 'running', 'expected': len(selected),
                  'started_at': datetime.now(timezone.utc).isoformat(), 'proofs': [], 'errors': []}
        write_verified(output, report)
        try:
            for index, row in enumerate(selected):
                learner, review = row['learner_id'], row['review_id']
                try:
                    items, pages, total = [], [], None
                    for page in range(1, 101):
                        values = {'userId': learner, 'itemId': review, 'itemType': '9', 'page': page,
                                  'pageSize': 100, 'take': 100, 'skip': (page - 1) * 100,
                                  'sort': 'CreateDateTime-desc', 'filter': '', 'group': ''}
                        for attempt in range(3):
                            response = session.post(settings.APTEM_BASE_URL.rstrip('/') +
                                '/MWS.eDocuments/DynamicDocuments/ReadComplianceDocuments', data=values,
                                timeout=(15, 60), allow_redirects=False)
                            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                                time.sleep(2 ** attempt)
                                continue
                            if response.status_code != 200:
                                raise ValueError('Document order HTTP ' + str(response.status_code))
                            data = response.json()
                            break
                        if (not isinstance(data.get('Data'), list) or type(data.get('Total')) is not int
                                or data.get('Errors') not in (None, [], {})):
                            raise ValueError('Invalid server order response')
                        if total is None:
                            total = data['Total']
                        if total != data['Total']:
                            raise ValueError('Document list changed during pagination')
                        items += [{'id': str(d['Id']), 'created_at_raw': d['CreateDateTime']} for d in data['Data']]
                        pages.append({'page': page, 'http_status': 200, 'count': len(data['Data'])})
                        if len(items) == total:
                            break
                        if not data['Data'] or len(items) > total:
                            raise ValueError('Document pagination incomplete')
                    if len(items) != total or len({d['id'] for d in items}) != total:
                        raise ValueError('Invalid ordered document identities')
                    report['proofs'].append({'aptem_learner_id': learner, 'aptem_review_id': review,
                        'sort': 'CreateDateTime-desc', 'checked_at': datetime.now(timezone.utc).isoformat(),
                        'complete': True, 'total': total, 'pages': pages, 'items': items})
                except Exception as exc:
                    report['errors'].append({'learner_id': learner, 'review_id': review,
                                             'error': type(exc).__name__})
                if (index + 1) % 25 == 0:
                    write_verified(output, report)
                    self.stdout.write(json.dumps({'document_order_done': index + 1,
                                                  'total': len(selected), 'errors': len(report['errors'])}))
                    self.stdout.flush()
                time.sleep(0.08)
        finally:
            session.close()
            report['state'] = 'complete' if len(report['proofs']) + len(report['errors']) == len(selected) else 'partial'
            write_verified(output, report)
        self.stdout.write(json.dumps({'document_order_done': len(selected), 'errors': len(report['errors']),
                                     'output': str(output)}))
