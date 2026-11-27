"""Modern-list membership for reviews and explicit Classic document identity.

Adds nullable columns only; no existing row is modified. These are populated by a
later backfill from the modern Planned/Finished inventory and the Classic
review-document mapping. Reversible: the reverse drops exactly what it adds.
"""
from django.db import migrations


def add_columns(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as c:
        # reviews: where each review sits in the modern Aptem Reviews lists.
        # source_review_program_id: the review's own programme (from OData).
        # modern_listing_program_id: the programme whose modern list showed it.
        # These can differ (main/sub programme or a historical episode); keep both.
        c.execute('''ALTER TABLE "Learner".reviews
            ADD COLUMN IF NOT EXISTS source_review_program_id bigint,
            ADD COLUMN IF NOT EXISTS modern_listing_program_id bigint,
            ADD COLUMN IF NOT EXISTS modern_bucket text
                CHECK (modern_bucket IS NULL OR modern_bucket IN ('planned','finished')),
            ADD COLUMN IF NOT EXISTS visibility_state text NOT NULL DEFAULT 'unchecked'
                CHECK (visibility_state IN ('unchecked','planned','finished','outside_modern_list')),
            ADD COLUMN IF NOT EXISTS visibility_checked_at timestamptz''')
        c.execute('CREATE INDEX IF NOT EXISTS reviews_modern_bucket_idx '
                  'ON "Learner".reviews(learner_id, modern_bucket)')
        c.execute('CREATE INDEX IF NOT EXISTS reviews_visibility_state_idx '
                  'ON "Learner".reviews(visibility_state)')
        # review_documents: the explicit Classic document identity. The existing
        # source_evidence_id is an Evidence id, not the Classic Document id.
        c.execute('''ALTER TABLE "Learner".review_documents
            ADD COLUMN IF NOT EXISTS aptem_document_id bigint,
            ADD COLUMN IF NOT EXISTS document_group_program_id bigint,
            ADD COLUMN IF NOT EXISTS document_created_at timestamptz,
            ADD COLUMN IF NOT EXISTS latest_selection_checked_at timestamptz''')
        c.execute('CREATE INDEX IF NOT EXISTS review_documents_aptem_doc_idx '
                  'ON "Learner".review_documents(aptem_document_id)')


def drop_columns(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as c:
        c.execute('DROP INDEX IF EXISTS "Learner".reviews_modern_bucket_idx')
        c.execute('DROP INDEX IF EXISTS "Learner".reviews_visibility_state_idx')
        c.execute('DROP INDEX IF EXISTS "Learner".review_documents_aptem_doc_idx')
        c.execute('''ALTER TABLE "Learner".reviews
            DROP COLUMN IF EXISTS source_review_program_id,
            DROP COLUMN IF EXISTS modern_listing_program_id,
            DROP COLUMN IF EXISTS modern_bucket,
            DROP COLUMN IF EXISTS visibility_state,
            DROP COLUMN IF EXISTS visibility_checked_at''')
        c.execute('''ALTER TABLE "Learner".review_documents
            DROP COLUMN IF EXISTS aptem_document_id,
            DROP COLUMN IF EXISTS document_group_program_id,
            DROP COLUMN IF EXISTS document_created_at,
            DROP COLUMN IF EXISTS latest_selection_checked_at''')


class Migration(migrations.Migration):
    dependencies = [('aptem', '0009_cacherefreshcontrol')]
    operations = [migrations.RunPython(add_columns, drop_columns)]
