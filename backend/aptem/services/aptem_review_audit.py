"""Offline, read-only comparison of the supplied Modern audit and stored IDs.

Run with python -m aptem.services.aptem_review_audit. Never imports Django settings,
opens a database, contacts Aptem, sends messages, or applies a deletion.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import html
import json
from pathlib import Path

from .aptem_review_contract import ContractError, identity, instant


def latest_profiles(rows):
    latest = {}
    for row in rows:
        key = identity(row['aptem_id'])
        stamp = instant(row['checked_at'])
        if key in latest and stamp == instant(latest[key]['checked_at']) and row != latest[key]:
            raise ContractError('Conflicting captures at the same time for learner ' + key)
        if key not in latest or stamp > instant(latest[key]['checked_at']):
            latest[key] = row
    return latest


def compare_profile(profile, stored):
    learner = identity(profile['aptem_id'])
    result = {'aptem_learner_id': learner, 'lms_learner_id': profile.get('lms_learner_id'),
              'checked_at': profile['checked_at'], 'source_state': profile['status'],
              'planned': [], 'finished': [], 'counts_by_type': {}, 'missing': [], 'outside': [],
              'duplicates': [], 'document_states': {}, 'ready_to_apply': False,
              'invalid_stored_identities': [],
              'blockers': ['Dependency checks and recovery plan are required before deletion.',
                           'Audit exports do not include per-page completeness proof and full source dates.']}
    existing = defaultdict(list)
    for row in stored:
        try:
            existing[identity(row['review_id'])].append(row)
        except ContractError:
            result['invalid_stored_identities'].append({'local_id': row.get('lms_review_id'),
                'reason': 'Stored identity is not a numeric Aptem Review ID', 'delete_allowed': False})
    result['duplicates'] = [{'aptem_review_id': rid, 'local_ids': [r['lms_review_id'] for r in values],
                             'decision': 'review_consolidation', 'delete_allowed': False}
                            for rid, values in existing.items() if len(values) > 1]
    if profile['status'] != 'ok':
        result['blockers'].append('No verified current Modern scope; no outside-list decisions.')
        return result
    if not isinstance(profile.get('planned'), list) or not isinstance(profile.get('finished'), list):
        raise ContractError('Modern audit lacks both lists for learner ' + learner)
    seen, types, doc_states = set(), Counter(), Counter()
    groups = profile.get('groups') or []
    for tab in ('planned', 'finished'):
        for row in profile[tab]:
            rid = identity(row['id'])
            if rid in seen:
                raise ContractError('Repeated source review across Modern tabs for learner ' + learner)
            seen.add(rid)
            if not row.get('status') or not row.get('type') or row.get('tab') != tab:
                raise ContractError('Modern audit row lacks consistent source state or tab')
            if 'enrol' in str(row['type']).lower():
                raise ContractError('Enrollment data is outside this review contract')
            result[tab].append(rid)
            types[str(row['type']).strip()] += 1
            if rid not in existing:
                result['missing'].append({'aptem_review_id': rid, 'tab': tab, 'source_status': row['status']})
            matching = [g for g in groups if str(g.get('review_id')) == rid and g.get('item_type') == 9]
            if len(matching) != 1:
                doc_states['group_missing' if not matching else 'group_ambiguous'] += 1
            elif matching[0].get('document_count') == 0:
                doc_states['reported_no_generated_document'] += 1
            else:
                doc_states['exact_group_latest_document_evidence_needed'] += 1
    result['outside'] = [dict(row, delete_allowed=False, decision='review_outside_candidate')
                         for rid, values in existing.items() if rid not in seen for row in values]
    result['counts_by_type'] = dict(types)
    result['document_states'] = dict(doc_states)
    return result


def build_report(profiles, stored_profiles, mapping=None, *, workers=4):
    latest = latest_profiles(profiles)
    stored = {}
    for profile in stored_profiles:
        key = identity(profile['aptem_id'])
        if key in stored:
            raise ContractError('Duplicate learner container in stored audit')
        stored[key] = profile['reviews']
    mapping_status = 'not_supplied'
    if mapping is not None:
        expected = {(learner, identity(row['id']), tab)
                    for learner, profile in latest.items() if profile['status'] == 'ok'
                    for tab in ('planned', 'finished') for row in profile[tab]}
        actual = [(identity(row['aptem_learner_id']), identity(row['aptem_review_id']), row['modern_tab'])
                  for row in mapping]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ContractError('Mapping CSV does not match the exact raw Modern inventory')
        mapping_status = 'exact'
    def compare(profile):
        return compare_profile(profile, stored.get(identity(profile['aptem_id']), []))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(compare, latest.values()))
    results.sort(key=lambda row: int(row['aptem_learner_id']))
    return {'mode': 'offline_dry_run', 'database_writes': 0, 'deletions': 0,
            'workers': workers, 'mapping_validation': mapping_status,
            'source_statuses': dict(Counter(row['source_state'] for row in results)),
            'counts': {'learners': len(results),
                'planned': sum(len(r['planned']) for r in results),
                'finished': sum(len(r['finished']) for r in results),
                'missing': sum(len(r['missing']) for r in results),
                'outside_candidates': sum(len(r['outside']) for r in results),
                'duplicate_groups': sum(len(r['duplicates']) for r in results)},
            'stored_learners_without_source_evidence': sorted(set(stored)-set(latest)),
            'learners': results}


def write_outputs(out, report):
    out.mkdir(parents=True, exist_ok=False)
    (out/'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    for key in ('outside', 'missing', 'duplicates'):
        with (out/(key+'.csv')).open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['aptem_learner_id', 'aptem_review_id', 'local_ids', 'source_status', 'decision', 'delete_allowed'])
            for learner in report['learners']:
                for row in learner[key]:
                    writer.writerow([learner['aptem_learner_id'], row.get('aptem_review_id', row.get('review_id')),
                        json.dumps(row.get('local_ids', [row.get('lms_review_id')])) if key != 'missing' else '',
                        row.get('source_status', row.get('status', '')), row.get('decision', 'fetch_missing'), 'false'])
    samples = [r for r in report['learners'] if r['aptem_learner_id'] in ('106', '16098', '3270', '4907')]
    rows = ''.join('<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in (
        r['aptem_learner_id'], r['source_state'], len(r['planned']) if r['source_state']=='ok' else 'Unknown',
        len(r['finished']) if r['source_state']=='ok' else 'Unknown', len(r['missing']), len(r['outside'])))+'</tr>' for r in samples)
    (out/'index.html').write_text('''<!doctype html><meta charset="utf-8"><title>Aptem Modern comparison</title>
        <style>body{font:16px system-ui;margin:3rem;max-width:1100px;color:#203044}td,th{padding:12px;border:1px solid #ccd4df}table{border-collapse:collapse}pre{white-space:pre-wrap;background:#eef3f9;padding:18px}</style>
        <h1>Aptem Modern — offline comparison</h1><p>Planned and Finished membership, not Classic grid membership.
        No database writes or deletions. Unknown scope is not an empty inventory.</p><pre>'''+
        html.escape(json.dumps(report['counts'],indent=2))+'''</pre><table><tr><th>Aptem learner</th><th>Source scope</th>
        <th>Planned</th><th>Finished</th><th>Missing in stored audit</th><th>Outside candidates</th></tr>'''+rows+'''
        </table><p><a href="outside.csv">Outside-list candidates</a> · <a href="missing.csv">Missing reviews</a> ·
        <a href="duplicates.csv">Duplicate identities</a> · <a href="report.json">Full report</a></p>
        <p>Outside-list candidates are not approved deletions. Check current source pagination, dependencies,
        snapshots and recovery before applying changes. Group signature flags do not identify latest-document signers.</p>''',encoding='utf-8')
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()}
    (out/'checksums.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-jsonl', type=Path, required=True)
    parser.add_argument('--stored-json', type=Path, required=True)
    parser.add_argument('--mapping-csv', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, choices=range(1,5), default=4)
    args = parser.parse_args()
    profiles = [json.loads(line) for line in args.live_jsonl.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    stored = json.loads(args.stored_json.read_text(encoding='utf-8-sig'))
    mapping = None
    if args.mapping_csv:
        with args.mapping_csv.open(encoding='utf-8-sig',newline='') as handle:
            mapping = list(csv.DictReader(handle))
    report = build_report(profiles, stored, mapping, workers=args.workers)
    report['input_sha256'] = {str(p):hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in (args.live_jsonl,args.stored_json,args.mapping_csv) if p}
    write_outputs(args.output,report)
    print(json.dumps({'report':str(args.output/'index.html'),**report['counts'],
                      'mapping_validation':report['mapping_validation'],'writes':0,'deletions':0}))


if __name__ == '__main__':
    main()
