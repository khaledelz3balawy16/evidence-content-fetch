from copy import deepcopy
from datetime import datetime, date, timedelta, timezone
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from aptem.services import aptem_review_contract as c, aptem_review_audit as audit


NOW = datetime(2026, 10, 24, 15, tzinfo=timezone.utc)


def bucket(items):
    return {'complete': True, 'total': len(items), 'items': items,
            'pages': [{'page': 1, 'http_status': 200, 'count': len(items)}]}


def inventory():
    return {'schema_version': 1, 'source': 'modern_planned_finished', 'instance': c.INSTANCE,
            'status': 'ok', 'aptem_learner_id': '106', 'lms_learner_id': '1117',
            'listing_program_id': '20260', 'run_id': 'fixture', 'checked_at': NOW.isoformat(),
            'planned': bucket([{'aptem_learner_id': '106', 'aptem_review_id': '23478',
                'source_program_id': '20260', 'source_status': 'In Progress', 'review_type': 'Progress Review',
                'planned_at': '2026-10-20T12:00:00+01:00', 'completed_at': None}]), 'finished': bucket([])}


def documents():
    docs = []
    for did, created in [('15290', '2026-07-31T13:37:11Z'), ('15292', '2026-07-31T13:38:54Z')]:
        docs.append({'aptem_document_id': did, 'created_at': created,
                     'source_url': f'https://{c.INSTANCE}/MWS.eDocuments/DynamicDocuments/GetFile?documentId={did}'})
    docs[1]['signatures'] = {'verified': True, 'roles_complete': True, 'aptem_document_id': '15292',
        'aptem_learner_id': '106', 'aptem_review_id': '23478', 'checked_at': NOW.isoformat(),
        'roles': [{'role': 'learner', 'required': True, 'verified': True, 'status': 'signed', 'name': 'Signed person'},
                  {'role': 'employer', 'required': True, 'verified': True, 'status': 'missing', 'follow_up_name': 'Contact'}]}
    return {'instance': c.INSTANCE, 'source': 'classic_review_documents', 'aptem_learner_id': '106',
            'aptem_review_id': '23478', 'checked_at': NOW.isoformat(), 'groups': bucket([
                {'processor': 'Review', 'aptem_review_id': '23478', 'document_group_program_id': '3921',
                 'documents': bucket(docs)}])}


def membership(snapshot=None):
    return c.plan_refresh([], snapshot or inventory(), now=NOW)[0]['metadata_patch'][c.MEMBERSHIP_KEY]


class ModernContractTests(SimpleTestCase):
    def test_failed_attempt_keeps_history_but_prevents_decisions_until_success(self):
        old = membership()
        failed = c.failed_membership_patch(old, '106', '23478', status='partial',
            checked_at=(NOW + timedelta(minutes=1)).isoformat(), run_id='failed', now=NOW)[c.MEMBERSHIP_KEY]
        self.assertTrue(failed['current'])
        self.assertEqual(failed['run_id'], old['run_id'])
        self.assertFalse(c.membership_view(failed, '106', '23478', now=NOW)['verified'])
        existing = [{'id': 7, 'aptem_learner_id': '106', 'aptem_review_id': '23478',
                     'review_data': {c.MEMBERSHIP_KEY: failed}}]
        with self.assertRaises(c.ContractError): c.plan_refresh(existing, inventory(), now=NOW)
        new = inventory(); new['checked_at'] = (NOW + timedelta(minutes=2)).isoformat()
        refreshed = c.plan_refresh(existing, new, now=NOW)[0]['metadata_patch']
        self.assertTrue(c.membership_view(refreshed[c.MEMBERSHIP_KEY], '106', '23478', now=NOW)['verified'])
        self.assertFalse(c.document_view(refreshed[c.DOCUMENT_KEY], '106', '23478', now=NOW)['verified'])

    def test_enrollment_is_out_of_scope(self):
        value = inventory(); value['planned']['items'][0]['review_type'] = 'Enrollment'
        with self.assertRaises(c.ContractError): c.validate_inventory(value, now=NOW)

    def test_in_progress_stays_planned_and_programs_do_not_define_identity(self):
        value = inventory()
        value['planned']['items'][0]['source_program_id'] = '55'
        existing = [{'id': 7, 'aptem_learner_id': '106', 'aptem_review_id': '23478', 'review_data': {}}]
        result = c.plan_refresh(existing, value, now=NOW)
        self.assertEqual(result[0]['local_id'], 7)
        self.assertEqual(result[0]['action'], 'update')
        self.assertEqual(result[0]['source']['tab'], 'planned')
        self.assertEqual(result[0]['source']['source_program_id'], '55')
        self.assertEqual(result[0]['source']['listing_program_id'], '20260')

    def test_no_current_program_and_transport_errors_never_archive(self):
        for status in ('no_current_program', 'error', 'current_program_unverified'):
            with self.subTest(status=status), self.assertRaises(c.ContractError):
                c.plan_refresh([], {**inventory(), 'status': status}, now=NOW)

    def test_complete_empty_tabs_are_valid_but_only_propose_archive(self):
        value = inventory(); value['planned'] = bucket([])
        result = c.plan_refresh([{'id': 7, 'aptem_learner_id': '106', 'aptem_review_id': '99'}], value, now=NOW)
        self.assertEqual(result[0]['action'], 'archive_candidate')
        self.assertEqual(result[0]['local_id'], 7)
        self.assertFalse(result[0]['metadata_patch'][c.MEMBERSHIP_KEY]['current'])

    def test_partial_pages_totals_failed_response_and_missing_dates_rejected(self):
        variants = []
        for changes in ({'complete': False}, {'total': 2}, {'pages': []},
                        {'pages': [{'page': 1, 'count': 1, 'http_status': 500}]}):
            value = inventory(); value['planned'].update(changes); variants.append(value)
        value = inventory(); del value['planned']['items'][0]['planned_at']; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(c.ContractError):
                c.validate_inventory(value, now=NOW)

    def test_cross_tab_duplicate_and_wrong_learner_rejected(self):
        value = inventory(); value['finished'] = deepcopy(value['planned'])
        with self.assertRaises(c.ContractError): c.validate_inventory(value, now=NOW)
        value = inventory(); value['planned']['items'][0]['aptem_learner_id'] = '107'
        with self.assertRaises(c.ContractError): c.validate_inventory(value, now=NOW)

    def test_duplicate_stored_records_do_not_get_silently_overwritten(self):
        rows = [{'id': i, 'aptem_learner_id': '106', 'aptem_review_id': '23478'} for i in (1,2)]
        with self.assertRaises(c.ContractError): c.plan_refresh(rows, inventory(), now=NOW)

    def test_older_run_cannot_replace_newer_snapshot(self):
        row = {'id': 1, 'aptem_learner_id': '106', 'aptem_review_id': '23478',
               'review_data': {c.MEMBERSHIP_KEY: {'checked_at': (NOW+timedelta(minutes=1)).isoformat()}}}
        with self.assertRaises(c.ContractError): c.plan_refresh([row], inventory(), now=NOW)

    def test_classic_grid_marker_is_not_modern_membership(self):
        view = c.membership_view({'source': 'aptem_classic_reviews', 'verified': True, 'visible': True}, '106', '23478', now=NOW)
        self.assertFalse(view['verified'])

    def test_stale_and_mismatched_membership_are_unknown(self):
        marker = membership()
        self.assertTrue(c.membership_view(marker, '106', '23478', now=NOW)['verified'])
        self.assertFalse(c.membership_view(marker, '107', '23478', now=NOW)['verified'])
        self.assertFalse(c.membership_view(marker, '106', '23478', now=NOW+timedelta(days=2))['verified'])

    def test_finished_does_not_mean_signed_or_completed(self):
        value = inventory(); row=value['planned']['items'][0]; row['source_status']='AwaitingSignature'
        row['completed_at']='2026-10-20T12:30:00+01:00'; value['finished']=value['planned'];value['planned']=bucket([])
        view=c.membership_view(membership(value), '106', '23478', now=NOW)
        self.assertTrue(view['verified'])
        self.assertEqual(view['source_status'], 'AwaitingSignature')
        self.assertEqual(view['tab'], 'finished')


class ClassicDocumentContractTests(SimpleTestCase):
    def test_latest_within_exact_group_and_separate_program(self):
        result=c.select_document(documents(),'106','23478',now=NOW)
        self.assertTrue(result['verified']);self.assertEqual(result['latest']['aptem_document_id'],'15292')
        self.assertEqual(result['document_group_program_id'],'3921')
        self.assertEqual(result['state'],'missing')
        self.assertEqual([r['role'] for r in result['missing_signers']],['employer'])
        self.assertEqual(len(result['versions']),2)

    def test_ambiguous_groups_or_latest_timestamp_tie_rejected(self):
        value=documents();value['groups']=bucket(value['groups']['items']*2)
        self.assertFalse(c.select_document(value,'106','23478',now=NOW)['verified'])
        value=documents();docs=value['groups']['items'][0]['documents']['items'];docs[0]['created_at']=docs[1]['created_at']
        self.assertFalse(c.select_document(value,'106','23478',now=NOW)['verified'])

    def test_unknown_timezone_and_missing_latest_date_not_guessed(self):
        for stamp in (None,'2026-07-31T14:38:54'):
            value=documents();value['groups']['items'][0]['documents']['items'][1]['created_at']=stamp
            self.assertFalse(c.select_document(value,'106','23478',now=NOW)['verified'])

    def test_wrong_learner_document_url_enrollment_and_incomplete_fetch_rejected(self):
        variants=[]
        value=documents();value['aptem_learner_id']='107';variants.append(value)
        value=documents();value['groups']['items'][0]['processor']='Enrollment';variants.append(value)
        value=documents();value['groups']['items'][0]['documents']['complete']=False;variants.append(value)
        for url in ('https://evil.example/a','javascript:alert(1)',
                    f'https://{c.INSTANCE}/MWS.eDocuments/DynamicDocuments/GetFile?documentId=999'):
            value=documents();value['groups']['items'][0]['documents']['items'][1]['source_url']=url;variants.append(value)
        for value in variants:
            with self.subTest(value=value):self.assertFalse(c.select_document(value,'106','23478',now=NOW)['verified'])

    def test_zero_documents_is_distinct_from_missing_group_and_missing_signature(self):
        value=documents();value['groups']['items'][0]['documents']=bucket([])
        result=c.select_document(value,'106','23478',now=NOW)
        self.assertTrue(result['verified']);self.assertEqual(result['state'],'document_not_generated')
        self.assertEqual(result['missing_signers'],[])
        value['groups']=bucket([])
        self.assertFalse(c.select_document(value,'106','23478',now=NOW)['verified'])

    def test_signatures_must_match_latest_document_and_have_complete_roles(self):
        value=documents();sig=value['groups']['items'][0]['documents']['items'][1]['signatures'];sig['aptem_document_id']='15290'
        self.assertFalse(c.select_document(value,'106','23478',now=NOW)['verified'])
        value=documents();value['groups']['items'][0]['documents']['items'][1]['signatures']['roles_complete']=False
        result=c.select_document(value,'106','23478',now=NOW)
        self.assertEqual(result['state'],'signature_unverified');self.assertEqual(result['missing_signers'],[])

    def test_checked_selection_is_recomputed_not_trusted(self):
        result=c.document_view({'selection':{'verified':True,'state':'signed'},'evidence':None},'106','23478',now=NOW)
        self.assertFalse(result['verified'])


class OfflineAuditTests(SimpleTestCase):
    def profile(self):
        return {'aptem_id':'106','lms_learner_id':'1117','checked_at':NOW.isoformat(),'status':'ok',
                'planned':[{'id':23478,'type':'Progress Review','status':'In Progress','tab':'planned'}],
                'finished':[],'groups':[]}

    def test_no_current_program_never_marks_history_outside(self):
        value=self.profile();value['status']='no_current_program'
        result=audit.compare_profile(value,[{'review_id':'99','lms_review_id':7}])
        self.assertEqual(result['outside'],[])

    def test_extra_duplicate_and_missing_lists_never_authorize_delete(self):
        rows=[{'review_id':'99','lms_review_id':i,'status':'Completed'} for i in (1,2)]
        result=audit.compare_profile(self.profile(),rows)
        self.assertEqual(len(result['duplicates']),1);self.assertEqual(len(result['outside']),2)
        self.assertEqual(result['missing'][0]['aptem_review_id'],'23478')
        self.assertFalse(result['ready_to_apply'])
        self.assertTrue(all(r['delete_allowed'] is False for r in result['outside']))

    def test_conflicting_capture_times_and_mapping_disagreement_fail(self):
        value=self.profile()
        with self.assertRaises(c.ContractError):audit.latest_profiles([value,{**value,'status':'error'}])
        with self.assertRaises(c.ContractError):audit.build_report([value],[],[],workers=1)

    def test_new_error_does_not_silently_reuse_earlier_success_for_deletion(self):
        value=self.profile();new={**value,'status':'error','checked_at':(NOW+timedelta(minutes=1)).isoformat()}
        report=audit.build_report([value,new],[{'aptem_id':'106','reviews':[{'review_id':'99','lms_review_id':1}]}])
        self.assertEqual(report['counts']['outside_candidates'],0)

    def test_invalid_synthetic_identity_is_blocked_not_deleted(self):
        result=audit.compare_profile(self.profile(),[{'review_id':'synthetic-test','lms_review_id':1}])
        self.assertEqual(len(result['invalid_stored_identities']),1);self.assertEqual(result['outside'],[])
