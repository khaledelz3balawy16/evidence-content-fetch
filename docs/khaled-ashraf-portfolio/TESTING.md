# Testing and validation

## Preparation checks completed

- Captured source at the recorded commit.
- Checked archive paths, symlinks, and file inclusion rules.
- Replaced detected credential literals, connection strings, and contact addresses with placeholders.
- Removed data exports, runtime environments, private keys, generated output, and internal assistant artifacts.
- Re-scanned the prepared text for the supported sensitive-value patterns.
- Parsed generated JSON documents and package manifests.

## Application validation

Dependencies were not installed. Application tests, servers, migrations, seeders, and database or external-service requests were not run. Runtime correctness after configuration and data removal remains to be verified by the project maintainer.

## Existing test sources

- `backend/aptem/management/commands/inspect_review_pdf_sources.py`
- `backend/aptem/tests/test_audit_review_content_capture.py`
- `backend/aptem/tests/test_evidence_feature.py`
- `backend/aptem/tests/test_modern_capture.py`
- `backend/aptem/tests/test_modern_document_order.py`
- `backend/aptem/tests/test_modern_review_contract.py`
- `backend/aptem/tests/test_modern_review_recovery.py`
- `backend/aptem/tests/test_modern_review_stage.py`
- `backend/aptem/tests/test_reconcile_preserved_review_sections.py`
- `backend/aptem/tests/test_restore_review_capture_backups.py`
- `backend/aptem/tests/test_review_api_sync.py`
- `backend/aptem/tests/test_review_audit.py`
- `backend/aptem/tests/test_review_content_repair.py`
- `backend/aptem/tests/test_review_pdf_archive.py`
- `backend/aptem/tests/test_review_repair.py`
- `backend/aptem/tests/test_review_signature_completion.py`
- `backend/aptem/tests/test_review_signatures.py`
- `backend/aptem/tests/test_review_storage.py`
- `backend/aptem/tests/test_review_ui_supplements.py`
- `backend/aptem/tests/test_review_visibility.py`
- `backend/aptem/tests/test_sync_visible_aptem_reviews.py`
- `backend/aptem/tests/test_visible_review_cycle.py`
