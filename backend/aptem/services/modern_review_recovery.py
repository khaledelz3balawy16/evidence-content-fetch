"""Compare-and-set recovery of Modern metadata from one learner journal.

Never deletes reviews or content. Inserted reviews remain with unverified
membership after recovery; later imports are rejected instead of overwritten.
"""
import json
from pathlib import Path

from django.db import connection, transaction

from .modern_review_stage import MEMBERSHIP_KEY, DOCUMENT_KEY, _id
from .review_signatures import load_verified, write_verified


def recovery_plan(journal, current):
    if journal.get('schema_version') != 2 or 'after' not in journal:
        raise ValueError('A completed schema-version-2 learner journal is required')
    learner, canonical = _id(journal['learner_id']), int(journal['lms_learner_id'])
    before = {row['id']: row for row in journal['before']}
    inserted = set(journal['inserted_ids'])
    after = journal['after']
    if len({row['id'] for row in after}) != len(after) or inserted & before.keys():
        raise ValueError('Duplicate or contradictory recovery identities')
    changes = []
    for row in after:
        pk = row['id']
        old = before.get(pk)
        if old is None and pk not in inserted:
            raise ValueError('Recovery row lacks a before image or an inserted identity')
        now = current.get(pk)
        if now is None:
            if pk in inserted:  # A rolled-back insert never became visible.
                continue
            raise ValueError('Existing recovery review is missing')
        if (now['learner_id'] != canonical or _id(now['aptem_review_id']) != _id(row['review_id'])
                or _id(row['membership']['aptem_learner_id']) != learner):
            raise ValueError('Recovery review identity changed')
        expected = {MEMBERSHIP_KEY: row['membership']}
        if row['write_document']:
            expected[DOCUMENT_KEY] = row['document']
        saved = now['metadata']
        restore = {}
        for key, value_key, present_key in (
                (MEMBERSHIP_KEY, 'modern_before', 'modern_present'),
                (DOCUMENT_KEY, 'document_before', 'document_present')):
            if key in expected:
                restore[key] = (bool(old and old[present_key]), old[value_key] if old else None)
        already_restored = all((key in saved) == present and (not present or saved[key] == value)
                               for key, (present, value) in restore.items())
        if already_restored:
            continue
        if any(key not in saved or saved[key] != value for key, value in expected.items()):
            raise ValueError('Review metadata changed after this journal; recovery refused')
        changes.append({'id': pk, 'review_id': row['review_id'], 'learner_id': canonical,
                        'expected': expected, 'restore': restore})
    return changes


def restore_journal(path, *, apply=False, backup_path=None):
    journal = load_verified(Path(path))
    with transaction.atomic(), connection.cursor() as cursor:
        if not apply:
            cursor.execute('SET TRANSACTION READ ONLY')
        cursor.execute("SELECT set_config('lock_timeout','5000ms',true),"
                       "set_config('statement_timeout','120000ms',true)")
        cursor.execute('SELECT aptem_id FROM "Learner".learners WHERE id=%s', [journal['lms_learner_id']])
        mapping = cursor.fetchone()
        if not mapping or _id(mapping[0]) != _id(journal['learner_id']):
            raise ValueError('Recovery learner mapping changed')
        ids = [row['id'] for row in journal.get('after', [])]
        cursor.execute('''SELECT id,learner_id,aptem_review_id,
            coalesce(review_data,'{}'::jsonb) FROM "Learner".reviews WHERE id=ANY(%s)'''
                       + (' FOR UPDATE' if apply else ''), [ids])
        current = {pk: {'learner_id': lms, 'aptem_review_id': rid,
                        'metadata': json.loads(meta) if isinstance(meta, str) else meta}
                   for pk, lms, rid, meta in cursor.fetchall()}
        changes = recovery_plan(journal, current)
        if apply:
            if not backup_path or Path(backup_path).exists():
                raise ValueError('A fresh recovery backup path is required')
            write_verified(Path(backup_path), {'journal': str(Path(path).resolve()),
                                               'before_recovery': current, 'plan': changes})
            for change in changes:
                patch = {key: value for key, (present, value) in change['restore'].items() if present}
                remove = [key for key, (present, _) in change['restore'].items() if not present]
                cursor.execute('''UPDATE "Learner".reviews
                    SET review_data=(coalesce(review_data,'{}'::jsonb) - %s::text[]) || %s::jsonb
                    WHERE id=%s AND learner_id=%s AND aptem_review_id::text=%s
                    AND review_data @> %s::jsonb''',
                    [remove, json.dumps(patch), change['id'], change['learner_id'],
                     change['review_id'], json.dumps(change['expected'])])
                if cursor.rowcount != 1:
                    raise ValueError('Review changed during recovery')
        return {'applied': apply, 'metadata_rows': len(changes), 'deleted_reviews': 0,
                'retained_inserted_reviews': len(journal.get('inserted_ids', []))}
