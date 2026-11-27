from copy import deepcopy
from django.test import SimpleTestCase
from aptem.services.aptem_review_contract import ContractError, server_order_winner, select_document


class DocumentOrderTests(SimpleTestCase):
    def fixture(self):
        documents = [{'aptem_document_id': str(i), 'created_at_raw': date,
                      'created_at': date, 'created_at_timezone_unresolved': True,
                      'source_url': 'https://kentbusinesscollege.aptem.co.uk/MWS.eDocuments/DynamicDocuments/GetFile?documentId='+str(i)}
                     for i, date in [(1,'2026-08-26T14:51:36'), (2,'2026-08-26T14:59:18')]]
        proof = {'sort': 'CreateDateTime-desc', 'aptem_learner_id': '10', 'aptem_review_id': '100',
                 'checked_at': '2026-10-24T12:00:00+00:00', 'complete': True, 'total': 2,
                 'pages': [{'page': 1, 'http_status': 200, 'count': 2}],
                 'items': [{'id': d['aptem_document_id'], 'created_at_raw': d['created_at_raw']}
                           for d in reversed(documents)]}
        return documents, proof

    def test_server_sort_selects_latest_without_inventing_timezone(self):
        docs, proof = self.fixture()
        envelope = {'source': 'classic_review_documents', 'instance': 'kentbusinesscollege.aptem.co.uk',
                    'aptem_learner_id': '10', 'aptem_review_id': '100',
                    'checked_at': '2026-10-24T12:00:00+00:00',
                    'groups': {'complete': True, 'total': 1, 'pages': [{'page': 1, 'count': 1, 'http_status': 200}],
                               'items': [{'processor': 'Review', 'aptem_review_id': '100',
                                          'document_group_program_id': '20',
                                          'documents': {**proof, 'items': docs, 'server_order': proof}}]}}
        result = select_document(envelope, '10', '100')
        self.assertTrue(result['verified'])
        self.assertEqual(result['latest']['aptem_document_id'], '2')
        self.assertFalse(result['latest']['timestamp_verified'])
        self.assertEqual(result['state'], 'signature_unverified')

    def test_different_version_set_or_identity_is_rejected(self):
        docs, proof = self.fixture()
        for change in ({'aptem_review_id': '101'}, {'total': 3}, {'sort': 'CreateDateTime-asc'}):
            with self.assertRaises(ContractError):
                server_order_winner({**proof, **change}, docs, '10', '100')
        altered = deepcopy(proof)
        altered['items'][0]['created_at_raw'] = '2026-09-01T00:00:00'
        with self.assertRaises(ContractError):
            server_order_winner(altered, docs, '10', '100')

    def test_tied_or_reversed_dates_remain_unresolved(self):
        docs, proof = self.fixture()
        proof['items'].reverse()
        with self.assertRaises(ContractError):
            server_order_winner(proof, docs, '10', '100')
        docs[1]['created_at_raw'] = docs[0]['created_at_raw']
        proof['items'][1]['created_at_raw'] = docs[0]['created_at_raw']
        with self.assertRaises(ContractError):
            server_order_winner(proof, docs, '10', '100')
