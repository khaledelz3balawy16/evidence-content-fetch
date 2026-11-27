الهدف
ابنِ نظام بيـ يسحب بيانات المتعلّمين والمراجعات (Reviews) والمستندات/الأدلة (Evidence) من منصّة Aptem الخاصة بـ Kent Business College، وبيـ يرفعها/يزامنها في قاعدة بيانات Postgres (Neon)، وبعدين بيـ يحمّل ملفات الأدلة ويستخرج نصّها ويحللها (reflection + KSBs).
الدومين (المصدر): https://kentbusinesscollege.aptem.co.uk
الوصول قراءة فقط من Aptem (login + GET/POST على endpoints للقراءة والترقيم/pagination). ممنوع أي كتابة أو حذف على Aptem.
قاعدة البيانات هي مصدر الحقيقة (source of truth)، والأعمدة فيها لازم تتحافظ.
المعمارية (3 أجزاء)
1) الـ Capture Worker (Node.js + Playwright, Chromium headless)بيعمل login على Aptem وبيسحب الجرد (inventory) بتاع كل متعلّم ويخزّنه ملفات JSON locally (مش بيكتب في الداتابيز مباشرة). بيشتغل على 1–4 workers بالتوازي، بـ pagination (pageSize=100)، مع retry/backoff، و checksums (sha256) لكل ملف، و manifest + progress.
الـ endpoints المستخدمة (نفسها بالظبط):
المفتاح	الميثود	الـ Endpoint	بيجيب
episodes	POST	/MWS.Review/Review/ReadReviewsEpisodes	برامج/حلقات المتعلّم
modern	GET	/api/MWS.Review/ReviewGrid/Get	الـ Reviews (الأساسي)
groups	POST	/MWS.eDocuments/DynamicDocuments/ReadReviewDocumentsGroups	مجموعات المستندات
documents	POST	/MWS.eDocuments/DynamicDocuments/ReadComplianceDocuments	المستندات
signatures	POST	/MWS.eDocuments/Signatures/SignaturesRead	التوقيعات
login	POST	/Users/Account/LogOn	تسجيل الدخول
الـ query الأساسي لسحب الـ Reviews (endpoint modern) — يتبعت بـ تابين:
planned: filter=(status~eq~'Not Scheduled'~or~status~eq~'Scheduled'~or~status~eq~'In Progress')
finished: filter=(status~eq~'Awaiting Signature'~or~status~eq~'Completed'~or~status~eq~'Archived'~or~status~eq~'Not Completed')
بارامترات مشتركة: userId, programId, sort, group, page, pageSize=100, skip, take=100.
استبعد أي صف نوعه Enrollment (أي TypeName فيه "enrol").
الـ لينك تحميل كل مستند بيتبني من الـ document Id:/MWS.eDocuments/DynamicDocuments/GetFile?documentId={Id}
2) الـ Reconciliation / Sync (Django + Postgres)بياخد الـ JSON المتحقَّق منه (checksums + proof) ويزامنه في schema "Learner" بمعاملات لكل متعلّم (per-learner transactions)، قابلة للاستعادة (journals/backup)، وبـ exact-ID readback. بيعدّل مفتاحين بس في الموجود (aptem_modern membership + aptem_classic_document) ويحافظ على التاريخ. لا حذف فيزيائي.
3) الـ Evidence Downloader + Analyzer (Django)بياخد الـ evidence_id، يبني لينك التحميل، يحمّل الملف، يستخرج النص، ويحلّله.
لينك تحميل الدليل: /MWS.LearningProgrammes/LearningItemEvidence/Document/{evidence_id} (HTTPS + hostname + prefix مقيّدين لأسباب أمان).
المسموح: pdf, doc, docx, png, jpg, txt. لو رجع HTML بتاع صفحة login ⇒ يعتبرها فشل مصادقة ويعمل auto-refresh للكوكي.
التخزين في APTEM_DOWNLOAD_DIRECTORY، وبعدها استخراج نص + تحليل (reflection strength, KSB detection) + حساب عدد الكلمات/الحروف.
التيبلز والسكيمة
أ) Schema "Learner" (المصدر الكنسي للمزامنة — raw SQL)
"Learner".learners
Plain Text
id, full_name, email, phone_number, lifecycle_status,
programme, programme_status, group_name, completed_hours, minimum_hours,
planned_hours, progress_hours, otjh_status, coach_name, coach_email, coach_rag,
start_date, end_date, aptem_id
"Learner".reviews
Plain Text
id, learner_id, aptem_review_id, review_name, review_type, reviewer_name,
planned_scheduled_date, completed_date, status, source_url, review_data (jsonb),
content_hash, extraction_status, last_error, learner_name, learner_email,
raw_text, last_extracted_at,
first_seen_at, last_seen_at, created_at, updated_at


review_data (jsonb) هو المخزن الأساسي — جواه aptem_modern (membership) وaptem_classic_document.
"Learner".review_sections
Plain Text
review_id, section_name, section_order, fields_json (jsonb), tables_json (jsonb), raw_text


ب) جداول تطبيق Django (الـ ORM)
AptemEvidence (ده التيبل اللي بياخد الـ id ويجيب اللينك):
Plain Text
evidence_id (unique), evidence_name, document_url,
status, status_code, content_type, content_disposition,
downloaded_file, downloaded_file_name, file_size_bytes,
word_count, character_count, text_extracted, extracted_text, extraction_note,
word_count_threshold, is_under_word_count_threshold,
quality_check_status, quality_check_summary,
analysis_status, has_reflection, reflection_strength, reflection_evidence (json),
has_ksbs, has_explicit_ksbs, has_implicit_ksbs, ksb_detection_type, ksb_codes (json),
knowledge_found, skills_found, behaviours_found, ksb_evidence (json), analysis_summary,
attempts, failure_reason, authentication_failed,
created_at, started_at, completed_at, updated_at
CacheRefreshControl (تنسيق دورة المزامنة المجدولة):
Plain Text
cache_name (unique), refresh_started_at, refreshed_at, last_error


إعدادات التشغيل (Environment variables)
Plain Text
APTEM_BASE_URL=https://kentbusinesscollege.aptem.co.uk
APTEM_LOGIN_EMAIL=...
APTEM_LOGIN_PASSWORD=...
APTEM_COOKIE_HEADER=...
APTEM_AUTO_REFRESH_COOKIE=True
APTEM_DOWNLOAD_DIRECTORY=media/aptem_evidence
APTEM_REQUEST_TIMEOUT_SECONDS=120
APTEM_REQUEST_DELAY_SECONDS=1.5
APTEM_MAX_RETRIES=2
APTEM_MAX_BATCH_SIZE=100
APTEM_MIN_WORD_COUNT=1000
APTEM_BROWSER_HEADLESS=true
# + DATABASE_URL لـ Postgres/Neon


متطلّبات/قيود مهمّة
قراءة فقط من Aptem، صفر كتابة/حذف هناك.
pagination لكل القوائم (pageSize=100)، مع التأكّد إن الإجمالي ما اتغيّرش أثناء الترقيم.
checksums (sha256) لكل ملف capture + manifest + progress.jsonl.
المزامنة idempotent: إعادة التشغيل تقرأ الحالة من جديد وتكتب التغييرات بس، بـ exact-ID readback.
التعامل مع انتهاء الجلسة (302/401/403 أو HTML بتاع login) كـ auth failure + محاولة refresh للكوكي.
احترام تحديد المعدّل (delay بين الطلبات + backoff على 408/429/5xx).
جاهز كده تنسخه. لو عايزني كمان أضيف أوامر التشغيل بالظبط (سطر تشغيل الـ Node worker بالـ flags: --credentials --candidates --output --workers --verify-ui، وأوامر Django management commands زي capture_modern_reviews / apply_modern_review_stage / process_source_evidence) أقولك وأزوّدها في قسم "التشغيل".