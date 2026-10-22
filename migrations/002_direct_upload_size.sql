-- LabExtract AI 002: allow direct (browser -> Storage) uploads up to 20 MB.
-- Configuration only: no table, column or policy changes. Run once in the Supabase SQL editor BEFORE deploying the
-- direct-upload version. Reversible: set file_size_limit back to 4194304 (4 MB).
-- Must match LABEXTRACT_MAX_DIRECT_UPLOAD_MB (default 20) in backend/uploads.py.

update storage.buckets
set file_size_limit = 20971520          -- 20 MB
where id = 'labextract-originals';

-- check: expect one row with 20971520
select id, file_size_limit, allowed_mime_types from storage.buckets where id = 'labextract-originals';
