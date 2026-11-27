"""Validate live local captures and build an isolated SQLite review snapshot.

No project settings, network calls, shared database access, or deletion path.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import sqlite3

from . import aptem_review_contract as contract
from .aptem_review_audit import build_report, write_outputs


def bucket(items, *, complete=True):
    return {'complete': complete, 'items': items, 'total': len(items),
            'pages': [{'page': 1, 'http_status': 200, 'count': len(items)}]}


def inventory(capture):
    value = {'schema_version': 1, 'source': 'modern_planned_finished', 'instance': contract.INSTANCE,
        'status': capture['status'], 'aptem_learner_id': str(capture['aptem_id']),
        'lms_learner_id': str(capture['lms_learner_id']), 'listing_program_id': str(capture['aptem_program_id']),
        'checked_at': capture['membership_checked_at'], 'run_id': capture['run_id']}
    for tab in ('planned', 'finished'):
        source = capture['modern_buckets'][tab]
        items = []
        for row in source['items']:
            if 'PlannedStartDateTime' not in row or 'CompleteDateTime' not in row:
                raise contract.ContractError('Missing source date fields')
            items.append({'aptem_learner_id': str(row['UserId']), 'aptem_review_id': str(row['Id']),
                'source_program_id': row.get('ProgramId'), 'review_type': row['TypeName'],
                'review_name': row.get('Name'), 'source_status': row['Status'],
                'planned_at': row['PlannedStartDateTime'], 'completed_at': row['CompleteDateTime'],
                'date_semantics': 'source_wall_clock'})
        value[tab] = {**source, 'items': items}
    return value


def corroborated_timestamp(raw, document_id, signatures):
    """Use an offset only when the exact document's signature row supplies it."""
    try:
        contract.instant(raw)
        return raw
    except contract.ContractError:
        pass
    matching = [r for r in signatures if str(r.get('DocumentId')) == str(document_id)]
    if len(matching) != 1:
        return raw
    supplied = matching[0].get('CreateDateTime')
    try:
        contract.instant(supplied)
        a = datetime.fromisoformat(str(raw))
        b = datetime.fromisoformat(str(supplied))
        if a.tzinfo is None and a.replace(microsecond=0) == b.replace(tzinfo=None, microsecond=0):
            return supplied
    except (contract.ContractError, TypeError, ValueError):
        pass
    return raw


def signatures_for(document_id, group, capture):
    learner, review = str(capture['aptem_id']), str(group['ItemId'])
    matches = [r for r in capture.get('signatures_bucket', {}).get('items', [])
               if str(r.get('DocumentId')) == str(document_id) and str(r.get('ItemId')) == review
               and str(r.get('EntityId')) == learner and r.get('ItemType') == 9]
    if len(matches) != 1:
        return None
    row = matches[0]
    contacts = {'learner': ('EntityName', 'learner'), 'coach': ('OwnerName', 'current_review_owner'),
                'employer': ('ManagerName', 'current_line_manager'), 'referrer': (None, 'current_referrer')}
    roles = []
    for role, prefix in {'learner': 'Participant', 'coach': 'Advisor', 'employer': 'Employer', 'referrer': 'Referrer'}.items():
        code = row.get(prefix + 'Signature')
        signer_id = row.get('ParticipantId' if role == 'learner' else prefix + 'SignerId')
        group_signer = group.get(prefix + 'SignerId')
        if type(code) is not int or code not in (0, 1, 2):
            return None
        if ((code == 1 and (not signer_id or signer_id != group_signer))
                or (code != 1 and (signer_id is not None or group_signer is not None))):
            return None
        name_key, basis = contacts[role]
        roles.append({'role': role, 'status': {0: 'missing', 1: 'signed', 2: 'not_required'}[code],
            'required': code != 2, 'verified': True, 'name': row.get(prefix + 'SignerName'),
            'signed_at': row.get(prefix + 'SignerDate'),
            'follow_up_name': row.get(name_key) if name_key else None, 'follow_up_basis': basis})
    missing = any(r['status'] == 'missing' for r in roles)
    if (missing and group.get('IsFullySigned') is True) or (not missing and group.get('IsFullySigned') is False):
        return None
    return {'verified': True, 'roles_complete': True, 'aptem_document_id': str(document_id),
        'aptem_learner_id': learner, 'aptem_review_id': review,
        'checked_at': capture.get('signatures_checked_at', capture['membership_checked_at']), 'roles': roles}


def documents(capture, orders=None):
    result = {}
    all_groups = [g for b in capture['groups_by_program'] for g in b['items'] if g.get('ItemType') == 9]
    # Missing programme pages can hide another matching group, so no selection is safe.
    complete = not any(e.get('program_id') for e in capture['document_errors'])
    signature_rows = capture.get('signatures_bucket', {}).get('items', [])
    for modern in capture['planned'] + capture['finished']:
        rid = str(modern['id'])
        groups = []
        for group in (g for g in all_groups if str(g['ItemId']) == rid):
            raw = capture['documents'].get(rid)
            converted = None
            if raw is not None:
                items = []
                for doc in raw['items']:
                    created = corroborated_timestamp(doc.get('CreateDateTime'), doc['Id'], signature_rows)
                    try:
                        contract.instant(created)
                        unresolved_zone = False
                    except contract.ContractError:
                        unresolved_zone = True
                    items.append({'aptem_document_id': str(doc['Id']), 'created_at_raw': doc.get('CreateDateTime'),
                        'created_at': created, 'created_at_timezone_unresolved': unresolved_zone,
                        'source_url': doc['source_url'], 'signatures': signatures_for(doc['Id'], group, capture)})
                converted = {**raw, 'items': items,
                    'complete': raw['complete'] and raw['total'] == group.get('DocCount')}
                proof = (orders or {}).get((str(capture['aptem_id']), rid))
                expected = {str(d['Id']): d['CreateDateTime'] for d in raw['items']}
                if proof is not None and ({str(d['id']): d['created_at_raw'] for d in proof['items']} != expected
                                          or len(proof['items']) != len(expected)):
                    proof = None
                if proof is None and raw.get('source_sort') == 'CreateDateTime-desc':
                    proof = {'aptem_learner_id': str(capture['aptem_id']), 'aptem_review_id': rid,
                             'sort': raw['source_sort'], 'checked_at': raw['checked_at'],
                             'complete': raw['complete'], 'total': raw['total'], 'pages': raw['pages'],
                             'items': [{'id': str(d['Id']), 'created_at_raw': d['CreateDateTime']}
                                       for d in raw['items']]}
                if proof is not None:
                    # Extra ordering evidence must describe this exact captured version set.
                    observed = {str(d['id']): d['created_at_raw'] for d in proof['items']}
                    if expected == observed and len(observed) == len(proof['items']):
                        converted['server_order'] = proof
            groups.append({'processor': 'Review', 'aptem_review_id': rid,
                           'document_group_program_id': str(group['ProgramId']), 'documents': converted})
        result[rid] = {'source': 'classic_review_documents', 'instance': contract.INSTANCE,
            'aptem_learner_id': str(capture['aptem_id']), 'aptem_review_id': rid,
            'checked_at': capture['checked_at'], 'groups': bucket(groups, complete=complete)}
    return result


def load_capture(folder):
    manifest_file = folder / 'manifest.json'
    manifest_body = manifest_file.read_bytes()
    if hashlib.sha256(manifest_body).hexdigest() != manifest_file.with_suffix('.json.sha256').read_text():
        raise contract.ContractError('Capture manifest checksum mismatch')
    manifest = json.loads(manifest_body)
    if manifest.get('state') != 'complete':
        raise contract.ContractError('Capture run did not finish')
    expected = set(manifest.get('expected_learner_ids') or [])
    if not expected:
        raise contract.ContractError('Capture manifest has no expected learner identities')
    records = []
    for file in sorted((folder / 'learners').glob('*.json')):
        body = file.read_bytes()
        if hashlib.sha256(body).hexdigest() != file.with_suffix('.json.sha256').read_text():
            raise contract.ContractError('Capture checksum mismatch: ' + file.name)
        record = json.loads(body)
        if (str(record.get('aptem_id')) != file.stem or record.get('run_id') != manifest.get('run_id')
                or str(record.get('aptem_id')) not in expected):
            raise contract.ContractError('Capture belongs to another learner or run')
        records.append(record)
    if {str(r['aptem_id']) for r in records} != expected:
        raise contract.ContractError('Capture run lacks one or more expected learner files')
    return records


def write_dashboard(output, db, summary):
    signers = {}
    for learner, review, role, status, name, contact, signed_at in db.execute('SELECT * FROM review_signers'):
        signers.setdefault((learner, review), []).append({'role': role, 'status': status,
            'name': name, 'contact': contact, 'signed_at': signed_at})
    rows = []
    for row in db.execute('''SELECT aptem_learner_id, aptem_review_id, modern_tab, source_status,
            planned_at, signature_state, latest_document_id, latest_document_url, review_json, metadata_json
            FROM current_reviews ORDER BY cast(aptem_learner_id AS INTEGER), cast(aptem_review_id AS INTEGER)'''):
        learner, review, tab, status, planned, signature, document, url, raw, metadata = row
        source = json.loads(raw)
        detail = json.loads(metadata)[contract.DOCUMENT_KEY]['selection']
        rows.append({'learner': learner, 'review': review, 'tab': tab, 'status': status, 'planned': planned,
            'signature': signature, 'document': document, 'url': url, 'type': source.get('TypeName'),
            'name': source.get('Name'), 'signers': signers.get((learner, review), []), 'reason': detail.get('reason')})
    data = json.dumps(rows, ensure_ascii=False).replace('<', '\\u003c')
    page = '''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8">
<title>مراجعات Aptem — النسخة المحلية</title><style>
body{font:16px system-ui;background:#f4f7fb;color:#203044;margin:30px}h1{margin-bottom:8px}
.cards{display:flex;gap:16px;flex-wrap:wrap;margin:22px 0}.card{background:white;padding:18px;border-radius:10px;min-width:160px}
.card strong{display:block;font-size:28px;color:#174fc1}input,select,button{font:inherit;padding:10px;border:1px solid #bbc8dc;border-radius:6px;margin:4px}
table{width:100%;border-collapse:collapse;background:white;margin-top:16px}td,th{padding:12px;text-align:right;border-bottom:1px solid #dce3ed;vertical-align:top}
.missing{color:#a42a15;font-weight:bold}.muted{color:#5b6c82;font-size:14px}a{color:#174fc1}.wrap{overflow:auto}small{display:block;margin-top:5px}
</style><h1>مراجعات Aptem — نسخة محلية</h1>
<p>بيانات سحب حي من Planned وFinished. حالة الحفظ والمطابقة بعد التحديث موجودة في <a href="../readback.json">تقرير التحقق من قاعدة البيانات</a>.</p>
<div class="cards">SUMMARY</div>
<p class="muted">المستند غير المحسوم لا يعني وجود توقيع ناقص. بيانات المستندات والأسماء المعروضة أدناه مرتبطة بنفس المتعلم والمراجعة والمستند. أسماء التواصل قد تكون اسم المسؤول الحالي وليست اسم موقّع سابق.</p>
<input id="search" placeholder="ابحث برقم المتعلم أو المراجعة أو الاسم" size="42">
<select id="state"><option value="missing">توقيع ناقص مؤكّد</option value="all">كل المراجعات الحالية</option value="unresolved">تحتاج تحققًا من المستند</option value="signed">التوقيعات مكتملة</option value="not_required">لا تتطلب توقيعًا</option value="document_not_generated">لم يُنشأ مستند</option value="signature_unverified">التوقيعات غير محسومة</option></select>
<select id="tab"><option value="all">Planned + Finished</option value="planned">Planned</option><option value="finished">Finished</option></select>
<p id="count"></p><div class="wrap"><table><thead><tr><th>المتعلم / المراجعة</th><th>النوع والحالة في Aptem</th><th>الموعد كما ورد من Aptem</th><th>التوقيعات / سبب التحقق</th><th>المستند</th></tr></thead><tbody id="rows"></tbody></table></div>
<button id="prev">السابق</button><button id="next">التالي</button>
<p><a href="missing-signatures.csv">تنزيل التوقيعات الناقصة CSV</a> · <a href="signers.csv">كل الموقّعين CSV</a> · <a href="index.html">تقرير فرق القوائم</a> · <a href="stage-summary.json">ملخص الفحص</a></p>
<script>const data=DATA;
const labels={learner:'الطالب',coach:'المدرب',employer:'صاحب العمل / المدير المباشر',referrer:'الجهة المحيلة'};
const statuses={signed:'وقّع',missing:'لم يوقّع',not_required:'التوقيع غير مطلوب'};
let page=0,filtered=[];const size=50;const byId=id=>document.getElementById(id);
function render(){const q=byId('search').value.toLowerCase().trim(),state=byId('state').value,tab=byId('tab').value;
filtered=data.filter(r=>(state==='all'||r.signature===state)&&(tab==='all'||r.tab===tab)&&JSON.stringify(r).toLowerCase().includes(q));
page=Math.max(0,Math.min(page,Math.ceil(filtered.length/size)-1));byId('count').textContent=`${filtered.length} مراجعة — صفحة ${page+1}`;byId('rows').replaceChildren();
for(const r of filtered.slice(page*size,(page+1)*size)){const tr=document.createElement('tr');
function cell(text){const td=document.createElement('td');td.textContent=text||'—';tr.appendChild(td);return td}
cell(`Learner ${r.learner} / Review ${r.review}`);cell(`${r.type}\n${r.status} — ${r.tab}`);cell(r.planned?.replace('T',' '));
const detail=cell('');detail.textContent='';for(const s of r.signers){const p=document.createElement('p');p.className=s.status==='missing'?'missing':'';p.textContent=`${labels[s.role]||s.role}: ${statuses[s.status]||s.status} — ${s.status==='missing'?(s.contact||'الاسم غير متاح'):(s.name||'الاسم غير متاح')}`;detail.appendChild(p)}
if(!r.signers.length)detail.textContent=r.reason||r.signature;
const doc=cell('');doc.textContent='';if(r.url){const a=document.createElement('a');a.href=r.url;a.target='_blank';a.rel='noreferrer';a.textContent=`فتح مستند ${r.document}`;doc.appendChild(a)}else doc.textContent='لا يوجد رابط أحدث مستند مؤكّد';byId('rows').appendChild(tr)}
byId('prev').disabled=page===0;byId('next').disabled=(page+1)*size>=filtered.length}
for(const id of ['search','state','tab'])byId(id).addEventListener('input',()=>{page=0;render()});byId('prev').onclick=()=>{page--;render()};byId('next').onclick=()=>{page++;render()};render();</script></html>'''
    cards = ''.join('<div class="card"><strong>' + html.escape(str(value)) + '</strong>' + label + '</div>'
                    for label, value in [('ملف متعلم مفحوص', summary['source_learners']),
                        ('مراجعة حالية محفوظة محليًا', summary['staged_current_reviews']),
                        ('شخص ناقص توقيعه مؤكّد', summary['confirmed_missing_roles']),
                        ('حالة مستند غير محسومة', summary['document_states'].get('unresolved', 0))])
    (output / 'dashboard.html').write_text(page.replace('SUMMARY', cards).replace('DATA', data), encoding='utf-8')


def stage(folder, stored_file, output, document_orders=None):
    records = load_capture(folder)
    orders = {}
    if document_orders is not None:
        body = document_orders.read_bytes()
        if hashlib.sha256(body).hexdigest() != document_orders.with_suffix('.sha256').read_text().strip():
            raise contract.ContractError('Document ordering checksum mismatch')
        proof = json.loads(body)
        if proof.get('state') != 'complete' or proof.get('source') != 'classic_server_order':
            raise contract.ContractError('Document ordering evidence did not complete')
        for value in proof['proofs']:
            key = str(value['aptem_learner_id']), str(value['aptem_review_id'])
            if key in orders:
                raise contract.ContractError('Duplicate document order evidence')
            orders[key] = value
    stored = json.loads(stored_file.read_text(encoding='utf-8-sig'))
    stored_by_learner = {str(p['aptem_id']): p['reviews'] for p in stored}
    report = build_report(records, stored)
    write_outputs(output, report)
    raw_path = output / 'live.jsonl'
    raw_path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
    db = sqlite3.connect(output / 'reviews.sqlite3')
    db.executescript('''
        CREATE TABLE learners (aptem_learner_id TEXT PRIMARY KEY, lms_learner_id TEXT, status TEXT, checked_at TEXT);
        CREATE TABLE current_reviews (
          aptem_learner_id TEXT, aptem_review_id TEXT, existing_local_id TEXT, listing_program_id TEXT,
          source_program_id TEXT, modern_tab TEXT, source_status TEXT, planned_at TEXT, completed_at TEXT,
          signature_state TEXT, latest_document_id TEXT, latest_document_url TEXT,
          review_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
          PRIMARY KEY(aptem_learner_id, aptem_review_id));
        CREATE TABLE review_signers (aptem_learner_id TEXT, aptem_review_id TEXT, role TEXT, status TEXT,
          signer_name TEXT, follow_up_name TEXT, signed_at TEXT,
          PRIMARY KEY(aptem_learner_id, aptem_review_id, role));
        CREATE VIEW missing_signatures AS SELECT * FROM review_signers WHERE status='missing';
    ''')
    errors, states, exact, stage_count = [], {}, True, 0
    now = datetime.now(timezone.utc)
    for capture in records:
        learner = str(capture['aptem_id'])
        db.execute('INSERT INTO learners VALUES (?,?,?,?)',
            (learner, str(capture.get('lms_learner_id') or ''), capture['status'], capture['checked_at']))
        if capture['status'] != 'ok':
            continue
        try:
            snapshot = inventory(capture)
            evidence = documents(capture, orders)
            current_ids = {str(r['id']) for r in capture['planned'] + capture['finished']}
            existing = [{'id': row['lms_review_id'], 'aptem_learner_id': learner,
                         'aptem_review_id': str(row['review_id'])}
                        for row in stored_by_learner.get(learner, []) if str(row['review_id']) in current_ids]
            actions = contract.plan_refresh(existing, snapshot, evidence, now=now)
            raw_by_id = {str(r['Id']): r for b in capture['modern_buckets'].values() for r in b['items']}
            rows = []
            for action in actions:
                metadata = action['metadata_patch']
                member = contract.membership_view(metadata[contract.MEMBERSHIP_KEY], learner, action['aptem_review_id'], now=now)
                if not member['verified']:
                    raise contract.ContractError(member['reason'])
                selection = contract.document_view(metadata[contract.DOCUMENT_KEY], learner, action['aptem_review_id'], now=now)
                rows.append((action, member, selection, metadata))
            if {a['aptem_review_id'] for a, _, _, _ in rows} != current_ids:
                raise contract.ContractError('Staged IDs differ from the live source')
            with db:
                for action, member, selection, metadata in rows:
                    rid = action['aptem_review_id']; latest = selection.get('latest') or {}
                    db.execute('INSERT INTO current_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                        learner, rid, str(action['local_id']) if action['local_id'] is not None else None,
                        member['listing_program_id'], member.get('source_program_id'), member['tab'],
                        member['source_status'], member['planned_at'], member['completed_at'], selection['state'],
                        latest.get('aptem_document_id'), latest.get('source_url'), json.dumps(raw_by_id[rid]), json.dumps(metadata)))
                    if selection['verified'] and selection['state'] in ('missing', 'signed', 'not_required'):
                        for role in selection['signers']:
                            db.execute('INSERT INTO review_signers VALUES (?,?,?,?,?,?,?)', (
                                learner, rid, role['role'], role['status'], role.get('name'), role.get('follow_up_name'), role.get('signed_at')))
                    states[selection['state']] = states.get(selection['state'], 0) + 1
                    stage_count += 1
        except (contract.ContractError, KeyError, ValueError) as error:
            errors.append({'aptem_learner_id': learner, 'error': str(error)})
            exact = False
    db.commit()
    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    for name, sql in [('missing-signatures', 'SELECT * FROM missing_signatures'),
                      ('signers', 'SELECT * FROM review_signers')]:
        cursor = db.execute(sql)
        with (output / (name + '.csv')).open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle); writer.writerow([c[0] for c in cursor.description])
            for row in cursor:
                writer.writerow(["'" + str(v) if str(v or '').startswith(('=', '+', '-', '@')) else v for v in row])
    missing_roles = db.execute('SELECT count(*) FROM missing_signatures').fetchone()[0]
    result = {'mode': 'live_capture_local_staging', 'source_learners': len(records),
        'source_states': report['source_statuses'], 'source_reviews': report['counts']['planned'] + report['counts']['finished'],
        'staged_current_reviews': stage_count, 'exact_current_ids': exact,
        'document_states': states, 'confirmed_missing_roles': missing_roles,
        'validation_errors': errors, 'sqlite_integrity': integrity,
        'shared_database_writes': 0, 'deletions': 0}
    write_dashboard(output, db, result)
    db.close()
    (output / 'stage-summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    hashes = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in output.iterdir() if f.is_file() and f.name != 'checksums.json'}
    (output / 'checksums.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--stored-json', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--document-orders', type=Path)
    args = parser.parse_args()
    print(json.dumps(stage(args.capture, args.stored_json, args.output, args.document_orders)))


if __name__ == '__main__':
    main()
