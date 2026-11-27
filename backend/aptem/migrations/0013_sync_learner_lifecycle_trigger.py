"""Postgres trigger: when learners.lifecycle_status changes,
auto-update reviews.learner_lifecycle_status for that learner.
"""
from django.db import migrations

SQL_CREATE = """
CREATE OR REPLACE FUNCTION "Learner".sync_review_lifecycle_status()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.lifecycle_status IS DISTINCT FROM OLD.lifecycle_status THEN
        UPDATE "Learner".reviews
        SET learner_lifecycle_status = NEW.lifecycle_status
        WHERE learner_id = NEW.id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_review_lifecycle_status ON "Learner".learners;

CREATE TRIGGER trg_sync_review_lifecycle_status
AFTER UPDATE OF lifecycle_status ON "Learner".learners
FOR EACH ROW
EXECUTE FUNCTION "Learner".sync_review_lifecycle_status();
"""

SQL_DROP = """
DROP TRIGGER IF EXISTS trg_sync_review_lifecycle_status ON "Learner".learners;
DROP FUNCTION IF EXISTS "Learner".sync_review_lifecycle_status();
"""


def apply(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as c:
        c.execute(SQL_CREATE)


def revert(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as c:
        c.execute(SQL_DROP)


class Migration(migrations.Migration):
    dependencies = [('aptem', '0012_review_learner_lifecycle_status')]
    operations = [migrations.RunPython(apply, revert)]
