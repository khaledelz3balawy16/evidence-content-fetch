"""Expose each review's current Classic (Review-documents) document as columns.

Nullable columns only; populated by the document-inventory backfill. Reversible.
"""
from django.db import migrations


def add_columns(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as c:
        c.execute('''ALTER TABLE "Learner".reviews
            ADD COLUMN IF NOT EXISTS current_document_id bigint,
            ADD COLUMN IF NOT EXISTS current_document_created_at timestamptz,
            ADD COLUMN IF NOT EXISTS current_document_program_id bigint,
            ADD COLUMN IF NOT EXISTS document_count integer,
            ADD COLUMN IF NOT EXISTS document_checked_at timestamptz''')
        c.execute('CREATE INDEX IF NOT EXISTS reviews_current_document_idx '
                  'ON "Learner".reviews(current_document_id)')


def drop_columns(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as c:
        c.execute('DROP INDEX IF EXISTS "Learner".reviews_current_document_idx')
        c.execute('''ALTER TABLE "Learner".reviews
            DROP COLUMN IF EXISTS current_document_id,
            DROP COLUMN IF EXISTS current_document_created_at,
            DROP COLUMN IF EXISTS current_document_program_id,
            DROP COLUMN IF EXISTS document_count,
            DROP COLUMN IF EXISTS document_checked_at''')


class Migration(migrations.Migration):
    dependencies = [('aptem', '0010_modern_membership_and_document_identity')]
    operations = [migrations.RunPython(add_columns, drop_columns)]
