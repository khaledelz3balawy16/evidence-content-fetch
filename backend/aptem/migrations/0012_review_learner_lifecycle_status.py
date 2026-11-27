"""Add learner_lifecycle_status column to reviews.

Denormalised snapshot of the learner's lifecycle_status at sync time,
so downstream projects can filter by learner status without joining learners.
Populated by backfill after migration; updated whenever learner status changes.
"""
from django.db import migrations


def add_column(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as c:
        c.execute('''ALTER TABLE "Learner".reviews
            ADD COLUMN IF NOT EXISTS learner_lifecycle_status varchar(50)''')
        c.execute('''CREATE INDEX IF NOT EXISTS reviews_learner_lifecycle_status_idx
            ON "Learner".reviews(learner_lifecycle_status)''')
        # Backfill from learners table
        c.execute('''UPDATE "Learner".reviews r
            SET learner_lifecycle_status = l.lifecycle_status
            FROM "Learner".learners l
            WHERE r.learner_id = l.id
              AND r.learner_lifecycle_status IS DISTINCT FROM l.lifecycle_status''')


def drop_column(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as c:
        c.execute('DROP INDEX IF EXISTS "Learner".reviews_learner_lifecycle_status_idx')
        c.execute('ALTER TABLE "Learner".reviews DROP COLUMN IF EXISTS learner_lifecycle_status')


class Migration(migrations.Migration):
    dependencies = [('aptem', '0011_review_current_document')]
    operations = [migrations.RunPython(add_column, drop_column)]
