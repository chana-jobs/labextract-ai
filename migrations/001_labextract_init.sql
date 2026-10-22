-- LabExtract AI: persistent storage for the product backend (Supabase Postgres + Storage).
-- Prepared locally; NOT applied. Apply only after approval, in the SQL Editor of the existing
-- Free project. It creates ONLY lx_* tables and the labextract-originals bucket; it does not touch
-- the cloud_spike resources (table spike_check, bucket spike-private). Synthetic data only.
--
-- Access model: the backend uses the service role (server side only). Row Level Security is
-- enabled on every table with NO policies, so the anon/authenticated keys can read or write nothing.
-- Workspace isolation is enforced by the backend on every query (workspace_id filter).

begin;

create table if not exists public.lx_workspaces (
    workspace_id text primary key,
    name         text not null,
    created_at   timestamptz not null default now()
);

-- One workspace per user in Stage A; users themselves live in Supabase Auth (auth.users).
create table if not exists public.lx_workspace_members (
    user_id      uuid not null references auth.users (id) on delete cascade,
    workspace_id text not null references public.lx_workspaces (workspace_id) on delete restrict,
    role         text not null default 'secretary' check (role in ('secretary', 'administrator', 'auditor')),
    created_at   timestamptz not null default now(),
    primary key (user_id, workspace_id)
);

create table if not exists public.lx_documents (
    file_id      text primary key check (file_id ~ '^[0-9a-f]{32}$'),
    workspace_id text not null references public.lx_workspaces (workspace_id) on delete restrict,
    metadata     jsonb not null,                -- returned by the API as is
    extension    text not null check (extension in ('.pdf', '.jpg', '.jpeg', '.png')),
    sha256       text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes   integer not null check (size_bytes > 0 and size_bytes <= 4194304),
    storage_path text not null unique,          -- <workspace_id>/<file_id><extension> in the private bucket
    created_at   timestamptz not null default now()
);
create index if not exists lx_documents_workspace_created on public.lx_documents (workspace_id, created_at desc);
create index if not exists lx_documents_workspace_sha256 on public.lx_documents (workspace_id, sha256);

create table if not exists public.lx_drafts (
    file_id      text primary key references public.lx_documents (file_id) on delete restrict,
    workspace_id text not null references public.lx_workspaces (workspace_id) on delete restrict,
    forms        jsonb not null,
    version      integer not null default 1 check (version >= 1),   -- optimistic concurrency
    updated_at   timestamptz not null default now()
);

-- Only a hash of the session token is stored; the token itself exists only in the user's cookie.
create table if not exists public.lx_sessions (
    token_hash   text primary key check (token_hash ~ '^[0-9a-f]{64}$'),
    user_id      uuid not null references auth.users (id) on delete cascade,
    workspace_id text not null references public.lx_workspaces (workspace_id) on delete cascade,
    created_at   timestamptz not null default now(),
    expires_at   timestamptz not null,
    revoked_at   timestamptz
);
create index if not exists lx_sessions_user on public.lx_sessions (user_id) where revoked_at is null;

alter table public.lx_workspaces        enable row level security;
alter table public.lx_workspace_members enable row level security;
alter table public.lx_documents         enable row level security;
alter table public.lx_drafts            enable row level security;
alter table public.lx_sessions          enable row level security;

-- Originals are private: no public URL; read and written by the backend with the service role only.
-- The bucket accepts exactly the content types the upload route verifies (backend/storage.py
-- CONTENT_TYPES) and the demo size limit (4 MB).
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('labextract-originals', 'labextract-originals', false, 4194304,
        array['application/pdf', 'image/jpeg', 'image/png'])
on conflict (id) do nothing;

commit;
