-- LabExtract AI 002: allow direct (browser -> Storage) uploads up to 20 MB.
-- Raises the 4 MB limit in BOTH places set by 001: the bucket and the lx_documents.size_bytes check.
-- No data is changed. Run in the Supabase SQL editor BEFORE using the direct-upload version.
-- Must match LABEXTRACT_MAX_DIRECT_UPLOAD_MB (default 20) in backend/uploads.py.
-- Rollback: the same statements with 4194304.

update storage.buckets
set file_size_limit = 20971520          -- 20 MB
where id = 'labextract-originals';

alter table public.lx_documents drop constraint if exists lx_documents_size_bytes_check;
alter table public.lx_documents
    add constraint lx_documents_size_bytes_check check (size_bytes > 0 and size_bytes <= 20971520);

-- check: expect 20971520, and the new check definition
select id, file_size_limit from storage.buckets where id = 'labextract-originals';
select pg_get_constraintdef(oid) from pg_constraint where conname = 'lx_documents_size_bytes_check';
