from copy import deepcopy
from django.test import SimpleTestCase
from aptem.services.modern_review_recovery import recovery_plan


class RecoveryTests(SimpleTestCase):
    def fixture(self):
        after = {'aptem_learner_id': '10', 'current': True}
        journal = {'schema_version': 2, 'learner_id': '10', 'lms_learner_id': 1,
                   'before': [{'id': 5, 'modern_before': None, 'document_before': {'old': 1},
                               'modern_present': False, 'document_present': True}],
                   'inserted_ids': [], 'after': [{'id': 5, 'review_id': '50', 'membership': after,
                                               'document': {'new': 2}, 'write_document': True}]}
        current = {5: {'learner_id': 1, 'aptem_review_id': '50',
                       'metadata': {'aptem_modern': after, 'aptem_classic_document': {'new': 2},
                                    'other': {'content': 'preserve'}}}}
        return journal, current

    def test_only_owned_metadata_restored_with_presence(self):
        journal, current = self.fixture()
        untouched = deepcopy(current)
        result = recovery_plan(journal, current)
        self.assertEqual(result[0]['restore'], {'aptem_modern': (False, None),
                                             'aptem_classic_document': (True, {'old': 1})})
        self.assertEqual(current, untouched)

    def test_concurrent_change_is_not_overwritten(self):
        journal, current = self.fixture()
        current[5]['metadata']['aptem_classic_document'] = {'later': 3}
        with self.assertRaisesRegex(ValueError, 'changed after'):
            recovery_plan(journal, current)

    def test_rolled_back_or_restored_journal_is_noop(self):
        journal, current = self.fixture()
        current[5]['metadata'] = {'aptem_classic_document': {'old': 1}}
        self.assertEqual(recovery_plan(journal, current), [])

    def test_insert_recovery_removes_membership_never_review(self):
        journal, current = self.fixture()
        journal['before'] = []
        journal['inserted_ids'] = [5]
        result = recovery_plan(journal, current)
        self.assertEqual(result[0]['restore'], {'aptem_modern': (False, None),
                                             'aptem_classic_document': (False, None)})
        self.assertEqual(recovery_plan(journal, {}), [])
