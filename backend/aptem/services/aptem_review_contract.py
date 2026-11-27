"""Pure Modern review reconciliation contract. No network, SQL, deletion or scheduling.

The server importer supplies complete, identity-bound snapshots; the app reads
the resulting metadata. Classic grid visibility is never Modern membership.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

VERSION = 1
MEMBERSHIP_KEY = 'aptem_modern'
DOCUMENT_KEY = 'aptem_classic_document'
INSTANCE = 'kentbusinesscollege.aptem.co.uk'
ROLES = {'learner', 'coach', 'employer', 'referrer'}


class ContractError(ValueError):
    pass


def identity(value):
    text = str(value or '')
    if not text.isdecimal() or int(text) <= 0 or isinstance(value, bool):
        raise ContractError('A positive source ID is required')
    return str(int(text))


def instant(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise ContractError('A timestamp is missing or invalid') from None
    if result.tzinfo is None:
        raise ContractError('An explicit source timezone is required')
    return result.astimezone(timezone.utc)


def checked(value, *, now=None, max_age_hours=None):
    stamp = instant(value)
    now = now or datetime.now(timezone.utc)
    if stamp > now + timedelta(minutes=5):
        raise ContractError('Source check is dated in the future')
    if max_age_hours is not None and now - stamp > timedelta(hours=max_age_hours):
        raise ContractError('Source snapshot is stale')
    return stamp


def source_date(value, semantics=None):
    """Preserve Aptem's explicit wall-clock dates without fabricating a UTC offset."""
    if semantics != 'source_wall_clock':
        return instant(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        raise ContractError('Invalid source wall-clock date') from None
    return parsed


def complete_rows(bucket):
    if not isinstance(bucket, dict) or bucket.get('complete') is not True:
        raise ContractError('Source pagination is incomplete')
    rows = bucket.get('items')
    pages = bucket.get('pages')
    if (not isinstance(rows, list) or type(bucket.get('total')) is not int
            or bucket['total'] != len(rows) or not isinstance(pages, list) or not pages):
        raise ContractError('Source total or page proof is invalid')
    if (any(not isinstance(p, dict) or p.get('http_status') != 200
            or p.get('page') != i + 1 or type(p.get('count')) is not int or p['count'] < 0
            for i, p in enumerate(pages)) or sum(p['count'] for p in pages) != len(rows)):
        raise ContractError('Source pages do not cover the inventory')
    return rows


def validate_inventory(snapshot, *, now=None):
    if (not isinstance(snapshot, dict) or snapshot.get('schema_version') != VERSION
            or snapshot.get('source') != 'modern_planned_finished'
            or snapshot.get('instance') != INSTANCE or snapshot.get('status') != 'ok'):
        raise ContractError('No complete Modern inventory; retain previous membership')
    learner = identity(snapshot.get('aptem_learner_id'))
    identity(snapshot.get('lms_learner_id'))
    program = identity(snapshot.get('listing_program_id'))
    checked(snapshot.get('checked_at'), now=now)
    if not isinstance(snapshot.get('run_id'), str) or not snapshot['run_id']:
        raise ContractError('A source run ID is required')
    selected = {}
    for tab in ('planned', 'finished'):
        for row in complete_rows(snapshot.get(tab)):
            if not isinstance(row, dict):
                raise ContractError('Malformed Modern review row')
            rid = identity(row.get('aptem_review_id'))
            if identity(row.get('aptem_learner_id')) != learner or rid in selected:
                raise ContractError('Conflicting learner or repeated review across Modern tabs')
            if not row.get('source_status') or not row.get('review_type'):
                raise ContractError('Review status and type must come from Aptem')
            if 'enrol' in str(row['review_type']).lower():
                raise ContractError('Enrollment is outside the review inventory contract')
            # null is an observed missing date; an omitted key is missing evidence.
            for key in ('planned_at', 'completed_at'):
                if key not in row:
                    raise ContractError('Review date evidence is missing')
                if row[key] is not None:
                    source_date(row[key], row.get('date_semantics'))
            if row.get('source_program_id') is not None:
                identity(row['source_program_id'])
            selected[rid] = {**row, 'aptem_review_id': rid, 'aptem_learner_id': learner,
                             'tab': tab, 'listing_program_id': program}
    return selected


def membership_view(value, learner_id, review_id, *, now=None, max_age_hours=24):
    result = {'verified': False, 'current': None, 'state': 'unknown', 'tab': None,
              'checked_at': None, 'listing_program_id': None, 'source_status': None,
              'planned_at': None, 'completed_at': None, 'date_semantics': None,
              'source_program_id': None, 'run_id': None, 'reason': ''}
    try:
        if not isinstance(value, dict):
            raise ContractError('Modern membership has not been synchronized')
        if (value.get('schema_version') != VERSION or value.get('source') != 'modern_planned_finished'
                or value.get('instance') != INSTANCE or value.get('verified') is not True
                or value.get('both_tabs_complete') is not True or not value.get('run_id')
                or type(value.get('current')) is not bool):
            raise ContractError('Modern membership is not verified')
        if (identity(value.get('aptem_learner_id')) != identity(learner_id)
                or identity(value.get('aptem_review_id')) != identity(review_id)):
            raise ContractError('Modern membership belongs to another learner or review')
        result.update({k: value.get(k) for k in ('checked_at', 'listing_program_id', 'tab', 'run_id',
                                               'source_status', 'planned_at', 'completed_at', 'source_program_id', 'date_semantics')})
        checked(value.get('checked_at'), now=now, max_age_hours=max_age_hours)
        attempt = value.get('last_attempt')
        if attempt is not None:
            if not isinstance(attempt, dict):
                raise ContractError('Malformed synchronization attempt')
            attempt_at = checked(attempt.get('checked_at'), now=now)
            if attempt_at >= instant(value['checked_at']) and attempt.get('status') != 'ok':
                raise ContractError('Latest Modern synchronization did not complete; retained history is unverified')
        identity(value.get('listing_program_id'))
        if value['current']:
            if value.get('tab') not in ('planned', 'finished') or not value.get('source_status'):
                raise ContractError('Current Modern row lacks its source tab or state')
            for key in ('planned_at', 'completed_at'):
                if key not in value:
                    raise ContractError('Modern date evidence is missing')
                if value[key] is not None:
                    source_date(value[key], value.get('date_semantics'))
        result.update(verified=True, current=value['current'],
                      state='current' if value['current'] else 'historical')
    except (ContractError, TypeError, AttributeError) as exc:
        result['reason'] = str(exc)
    return result


def document_url(url, document_id):
    parsed = urlparse(str(url or ''))
    return bool(parsed.scheme == 'https' and parsed.hostname == INSTANCE
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and not parsed.fragment and parsed.path == '/MWS.eDocuments/DynamicDocuments/GetFile'
                and parse_qs(parsed.query).get('documentId') == [identity(document_id)])


def server_order_winner(proof, documents, learner_id, review_id, *, now=None):
    """Use Aptem's date sort without inventing offsets for its wall-clock fields."""
    if (not isinstance(proof, dict) or proof.get('sort') != 'CreateDateTime-desc'
            or identity(proof.get('aptem_learner_id')) != identity(learner_id)
            or identity(proof.get('aptem_review_id')) != identity(review_id)):
        raise ContractError('Document ordering identity is invalid')
    checked(proof.get('checked_at'), now=now)
    rows = complete_rows(proof)
    ids = [identity(row.get('id')) for row in rows]
    expected = {identity(d['aptem_document_id']): d.get('created_at_raw') for d in documents}
    if (len(ids) != len(set(ids)) or set(ids) != set(expected)
            or any(row.get('created_at_raw') != expected[did] for row, did in zip(rows, ids))):
        raise ContractError('Document ordering describes another version set')
    dates = [datetime.fromisoformat(str(row['created_at_raw']).replace('Z', '+00:00')) for row in rows]
    if not dates or any(dates[i] < dates[i + 1] for i in range(len(dates) - 1)):
        raise ContractError('Aptem did not return descending creation dates')
    if len(dates) > 1 and dates[0] == dates[1]:
        raise ContractError('Latest server document dates are tied')
    return next(i for i, d in enumerate(documents) if identity(d['aptem_document_id']) == ids[0])


def select_document(envelope, learner_id, review_id, *, now=None):
    """Select within an explicit Review group, never by type/date similarity."""
    result = {'schema_version': VERSION, 'instance': INSTANCE,
              'aptem_learner_id': identity(learner_id), 'aptem_review_id': identity(review_id),
              'state': 'unresolved', 'verified': False, 'reason': '', 'latest': None,
              'versions': [], 'checked_at': None, 'missing_signers': [], 'signers': []}
    try:
        if not isinstance(envelope, dict) or envelope.get('source') != 'classic_review_documents':
            raise ContractError('Exact Classic document evidence is unavailable')
        if (envelope.get('instance') != INSTANCE
                or identity(envelope.get('aptem_learner_id')) != identity(learner_id)
                or identity(envelope.get('aptem_review_id')) != identity(review_id)):
            raise ContractError('Classic document inventory identity mismatch')
        stamp = checked(envelope.get('checked_at'), now=now)
        result['checked_at'] = envelope['checked_at']
        groups = complete_rows(envelope.get('groups'))
        matches = [g for g in groups if g.get('processor') == 'Review'
                   and str(g.get('aptem_review_id')) == identity(review_id)]
        if len(matches) != 1:
            raise ContractError('Classic Review group is missing or ambiguous')
        group = matches[0]
        program = identity(group.get('document_group_program_id'))
        documents = complete_rows(group.get('documents'))
        result['document_group_program_id'] = program
        if not documents:
            return {**result, 'verified': True, 'state': 'document_not_generated'}
        ids = [identity(d.get('aptem_document_id')) for d in documents]
        if len(ids) != len(set(ids)):
            raise ContractError('Duplicate Classic document identity')
        versions, bounds = [], []
        for doc, did in zip(documents, ids):
            try:
                created = instant(doc.get('created_at'))
                lower, upper, exact = created, created, True
            except ContractError:
                if doc.get('created_at_timezone_unresolved') is not True:
                    raise
                wall = datetime.fromisoformat(str(doc.get('created_at')))
                if wall.tzinfo is not None:
                    raise ContractError('Invalid unresolved document timestamp')
                # ISO datetime offsets are strictly within +/-24 hours. Preserve
                # the unknown offset; these are comparison bounds, not a UTC conversion.
                centre = wall.replace(tzinfo=timezone.utc)
                lower, upper, exact = centre - timedelta(hours=24), centre + timedelta(hours=24), False
            if lower > stamp:
                raise ContractError('Document is newer than its source inventory')
            if not document_url(doc.get('source_url'), did):
                raise ContractError('Document URL does not identify the exact Classic document')
            versions.append({'aptem_document_id': did, 'created_at': doc['created_at'],
                             'source_url': doc['source_url'], 'timestamp_verified': exact})
            bounds.append((lower, upper, exact))
        winners = [i for i, (lower, upper, exact) in enumerate(bounds)
                   if exact and all(i == j or lower > other[1] for j, other in enumerate(bounds))]
        ordering = group['documents'].get('server_order')
        if ordering is not None:
            winner = server_order_winner(ordering, documents, learner_id, review_id, now=now)
            if winners and winners != [winner]:
                raise ContractError('Server ordering contradicts exact document dates')
            winners = [winner]
            result['latest_selection_basis'] = 'aptem_server_created_date_desc'
        if len(winners) != 1:
            raise ContractError('Latest document timestamps overlap, are tied, or lack an exact offset')
        latest = [versions[winners[0]]]
        newest = bounds[winners[0]][1]
        result.update(verified=True, state='signature_unverified', latest=latest[0], versions=versions)
        doc = documents[ids.index(latest[0]['aptem_document_id'])]
        signatures = doc.get('signatures')
        if not isinstance(signatures, dict) or signatures.get('verified') is not True:
            return result
        if (identity(signatures.get('aptem_document_id')) != latest[0]['aptem_document_id']
                or identity(signatures.get('aptem_learner_id')) != identity(learner_id)
                or identity(signatures.get('aptem_review_id')) != identity(review_id)):
            raise ContractError('Signature evidence belongs to another document or review')
        sig_stamp = checked(signatures.get('checked_at'), now=now)
        if sig_stamp < newest:
            raise ContractError('Signature evidence predates this document')
        roles = signatures.get('roles')
        if signatures.get('roles_complete') is not True or not isinstance(roles, list) or not roles:
            return result
        seen = set()
        for role in roles:
            name = role.get('role')
            if (name not in ROLES or name in seen or role.get('verified') is not True
                    or role.get('status') not in ('signed', 'missing', 'not_required', 'unknown')
                    or (role.get('status') in ('signed', 'missing') and role.get('required') is not True)
                    or (role.get('status') == 'not_required' and role.get('required') is not False)):
                raise ContractError('Signature role requirements are incomplete or contradictory')
            seen.add(name)
        result['signers'] = [{k: role.get(k) for k in ('role', 'status', 'required', 'name', 'signed_at', 'follow_up_name')}
                             for role in roles]
        result['signature_checked_at'] = signatures['checked_at']
        if any(r['status'] == 'unknown' for r in roles):
            return result
        result['missing_signers'] = [r for r in result['signers'] if r['status'] == 'missing']
        result['state'] = 'missing' if result['missing_signers'] else (
            'signed' if any(r['status'] == 'signed' for r in roles) else 'not_required')
    except (ContractError, TypeError, AttributeError, ValueError) as exc:
        result.update(verified=False, state='unresolved', reason=str(exc), latest=None,
                      signers=[], missing_signers=[])
    return result


def document_view(metadata, learner_id, review_id, *, now=None, max_age_hours=24):
    evidence = metadata.get('evidence') if isinstance(metadata, dict) else None
    try:
        selected = select_document(evidence, learner_id, review_id, now=now)
    except ContractError as exc:
        return {'verified': False, 'state': 'unresolved', 'reason': str(exc),
                'latest': None, 'checked_at': None, 'missing_signers': [], 'signers': []}
    try:
        if selected.get('verified'):
            checked(selected['checked_at'], now=now, max_age_hours=max_age_hours)
            if selected.get('signature_checked_at'):
                checked(selected['signature_checked_at'], now=now, max_age_hours=max_age_hours)
    except ContractError as exc:
        selected.update(verified=False, state='unresolved', reason=str(exc),
                        latest=None, signers=[], missing_signers=[])
    return selected


def failed_membership_patch(previous, learner_id, review_id, *, status, checked_at, run_id, now=None):
    """Retain the last inventory, but suspend decisions after a failed new attempt."""
    if status not in {'error', 'partial', 'no_current_program'} or not run_id:
        raise ContractError('An unsuccessful source attempt and run ID are required')
    stamp = checked(checked_at, now=now)
    previous = dict(previous or {})
    for key, value in (('aptem_learner_id', learner_id), ('aptem_review_id', review_id)):
        if previous.get(key) and identity(previous[key]) != identity(value):
            raise ContractError('Failed attempt identity mismatch')
        previous[key] = identity(value)
    last = previous.get('last_attempt') or {}
    for earlier in (previous.get('checked_at'), last.get('checked_at')):
        if earlier and instant(earlier) > stamp:
            raise ContractError('Cannot replace a newer synchronization attempt')
    previous['last_attempt'] = {'status': status, 'checked_at': checked_at, 'run_id': run_id}
    return {MEMBERSHIP_KEY: previous}


def plan_refresh(existing, snapshot, documents=None, *, now=None):
    """Return review metadata patches; never execute them or delete a record.

    Stable identities are learner+review. A changed programme updates metadata,
    never the local primary key. Worker must commit membership per learner only
    after both tabs and all required new rows have been persisted successfully.
    """
    selected = validate_inventory(snapshot, now=now)
    learner = identity(snapshot['aptem_learner_id'])
    by_id = {}
    for row in existing:
        if identity(row.get('aptem_learner_id')) != learner:
            raise ContractError('Stored review belongs to another learner')
        rid = identity(row.get('aptem_review_id'))
        if rid in by_id:
            raise ContractError('Duplicate stored identities require reviewed consolidation')
        previous = (row.get('review_data') or {}).get(MEMBERSHIP_KEY) or {}
        if previous.get('checked_at') and instant(previous['checked_at']) > instant(snapshot['checked_at']):
            raise ContractError('Cannot overwrite a newer membership snapshot')
        attempt = previous.get('last_attempt') or {}
        if attempt.get('checked_at') and instant(attempt['checked_at']) > instant(snapshot['checked_at']):
            raise ContractError('Cannot overwrite a newer synchronization attempt')
        by_id[rid] = row
    actions = []
    for rid in sorted(set(selected) | set(by_id), key=int):
        row = selected.get(rid)
        membership = {'schema_version': VERSION, 'source': 'modern_planned_finished', 'instance': INSTANCE,
            'aptem_learner_id': learner, 'aptem_review_id': rid, 'run_id': snapshot['run_id'],
            'checked_at': snapshot['checked_at'], 'verified': True, 'both_tabs_complete': True,
            'current': row is not None, 'listing_program_id': str(snapshot['listing_program_id']),
            'tab': row['tab'] if row else None}
        if row:
            membership.update({k: row.get(k) for k in ('source_status', 'planned_at', 'completed_at', 'source_program_id', 'date_semantics')})
        patches = {MEMBERSHIP_KEY: membership}
        if row:
            # Keep exact document evidence alongside the selection for independent
            # consumer validation. A failed new check must not serve an old signature.
            envelope = (documents or {}).get(rid)
            selection = select_document(envelope, learner, rid, now=now)
            patches[DOCUMENT_KEY] = {'evidence': envelope, 'selection': selection}
        actions.append({'action': 'update' if row and rid in by_id else 'insert' if row else 'archive_candidate',
                        'local_id': by_id.get(rid, {}).get('id'), 'aptem_review_id': rid,
                        'aptem_learner_id': learner, 'source': row, 'metadata_patch': patches})
    return actions
