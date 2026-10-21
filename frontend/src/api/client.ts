/**
 * LabExtract AI — API client (native fetch, no Axios).
 *
 * Real requests use the existing FastAPI contracts. Mock calls stay isolated
 * behind VITE_USE_MOCK and are announced in the UI.
 */

import type {
  ApprovalStatus,
  DocumentMeta,
  EngineResult,
  ExportSummary,
  FieldName,
  Form,
} from "../types/contracts";

export { ApiError } from "./errors";
import { ApiError } from "./errors";

import {
  mockListDocuments,
  mockGetDocument,
  mockUploadDocument,
  mockInitDrafts,
  mockGetDrafts,
  mockSaveField,
  mockApproveField,
  mockGetExportSummary,
  mockDownloadExcel,
  mockLogin,
  mockGetSession,
  mockLogout,
} from "./mockLayer";

// ----------------------------------------------------------------- config

const USE_MOCK = import.meta.env.VITE_USE_MOCK === "true";
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";
let authSessionGeneration = 0;
const authRequests = new Set<AbortController>();

export function getAuthSessionGeneration(): number {
  return authSessionGeneration;
}

export function isAuthSessionCurrent(generation: number): boolean {
  return generation === authSessionGeneration;
}

export function invalidateAuthSession(): void {
  authSessionGeneration += 1;
  for (const controller of authRequests) controller.abort();
  authRequests.clear();
}

export function beginAuthSession(): number {
  invalidateAuthSession();
  return authSessionGeneration;
}

export interface SessionPrincipal {
  user_id: string;
  workspace_id: string;
}

// ----------------------------------------------------------------- low-level fetch wrapper

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options: { notifyUnauthorized?: boolean } = {}
): Promise<T> {
  const generation = authSessionGeneration;
  const init: RequestInit = {
    method,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
    },
  };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
  }

  const res = await fetchInAuthSession(`${BASE_URL}${path}`, init);
  if (!isAuthSessionCurrent(generation)) {
    throw new DOMException("Authentication session changed", "AbortError");
  }

  if (!res.ok) {
    let errorBody: unknown;
    try { errorBody = await res.json(); }
    catch { errorBody = await res.text(); }
    if (!isAuthSessionCurrent(generation)) {
      throw new DOMException("Authentication session changed", "AbortError");
    }
    if (res.status === 401 && options.notifyUnauthorized !== false) {
      window.dispatchEvent(new Event("labextract:unauthorized"));
    }
    throw new ApiError(res.status, errorBody, `HTTP ${res.status}: ${path}`);
  }

  if (res.status === 204) return undefined as unknown as T;
  const data = await res.json() as T;
  if (!isAuthSessionCurrent(generation)) {
    throw new DOMException("Authentication session changed", "AbortError");
  }
  return data;
}

async function fetchInAuthSession(url: string, init: RequestInit): Promise<Response> {
  const generation = authSessionGeneration;
  const controller = new AbortController();
  authRequests.add(controller);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    if (!isAuthSessionCurrent(generation)) {
      throw new DOMException("Authentication session changed", "AbortError");
    }
    return response;
  } finally {
    authRequests.delete(controller);
  }
}

// ----------------------------------------------------------------- API surface

/** GET /health */
export async function getHealth(): Promise<{ status: string }> {
  if (USE_MOCK) return { status: "ok (mock)" };
  return request("GET", "/health");
}

/** GET /documents */
export async function listDocuments(): Promise<DocumentMeta[]> {
  if (USE_MOCK) return mockListDocuments();
  return request("GET", "/documents");
}

export async function login(email: string, password: string): Promise<SessionPrincipal> {
  if (USE_MOCK) return mockLogin(email, password);
  return request("POST", "/auth/login", { email, password }, { notifyUnauthorized: false });
}

export async function getSession(): Promise<SessionPrincipal> {
  if (USE_MOCK) return mockGetSession();
  return request("GET", "/auth/me");
}

export async function logout(): Promise<void> {
  if (USE_MOCK) return mockLogout();
  return request("POST", "/auth/logout");
}

/** GET /documents/:fileId */
export async function getDocument(fileId: string): Promise<DocumentMeta> {
  if (USE_MOCK) return mockGetDocument(fileId);
  return request("GET", `/documents/${fileId}`);
}

/** POST /documents/upload — upload PDF/JPG/PNG (multipart/form-data) */
export async function uploadDocument(file: File): Promise<DocumentMeta> {
  if (USE_MOCK) return mockUploadDocument(file);
  const generation = authSessionGeneration;
  const form = new FormData();
  form.append("file", file);
  const res = await fetchInAuthSession(`${BASE_URL}/documents/upload`, {
    method: "POST",
    credentials: "include",
    body: form,
  });
  if (!isAuthSessionCurrent(generation)) {
    throw new DOMException("Authentication session changed", "AbortError");
  }
  if (!res.ok) {
    let errorBody: unknown;
    try { errorBody = await res.json(); } catch { errorBody = await res.text(); }
    if (!isAuthSessionCurrent(generation)) {
      throw new DOMException("Authentication session changed", "AbortError");
    }
    if (res.status === 401) window.dispatchEvent(new Event("labextract:unauthorized"));
    throw new ApiError(res.status, errorBody, `Upload failed: HTTP ${res.status}`);
  }
  const document = await res.json() as DocumentMeta;
  if (!isAuthSessionCurrent(generation)) {
    throw new DOMException("Authentication session changed", "AbortError");
  }
  return document;
}

/** POST /documents/:fileId/drafts/init */
export async function initDrafts(fileId: string): Promise<void> {
  if (USE_MOCK) return mockInitDrafts(fileId);
  return request("POST", `/documents/${fileId}/drafts/init`);
}

/** GET /documents/:fileId/drafts */
export async function getDrafts(fileId: string): Promise<EngineResult> {
  if (USE_MOCK) return mockGetDrafts(fileId);
  const forms = await request<Form[]>("GET", `/documents/${fileId}/drafts`);
  const form = forms[0];
  if (!form) throw new ApiError(422, { error: "NO_FORMS" }, "No review forms were returned");
  return {
    engine_run: { run_id: "server", engine_version: "backend", mode: "LOCAL_ONLY" },
    form,
    forms,
  };
}

// ----------------------------------------------------------------- Save (no approval)

/**
 * Save a field value only. Does NOT approve.
 * Approval_status stays PENDING on the server.
 *
 * Backend endpoint: PATCH /review/:fileId/:formId/field
 * Uses a stable target and expected_version for optimistic locking.
 */
export async function saveField(
  fileId: string,
  formId: string,
  examinationId: string,
  commitmentId: string | null,
  fieldName: FieldName,
  normalized: string,
  version: number,
  targetDetails: { index?: number; expected_value?: string } = {}
): Promise<{ saved: boolean; normalized: string; version: number }> {
  if (USE_MOCK) {
    return mockSaveField(fileId, formId, examinationId, commitmentId, fieldName, normalized, version, targetDetails);
  }

  const raw = await request<{ changed: boolean; field: { version: number } | null }>(
    "PATCH",
    `/review/${fileId}/${formId}/field`,
    {
      target: buildTarget(fieldName, commitmentId, targetDetails),
      op: "set",
      value: normalized,
      expected_version: version,
    }
  );
  const newVersion = raw.field?.version ?? version;
  void examinationId;
  return { saved: raw.changed, normalized, version: newVersion };
}

// ----------------------------------------------------------------- Approve (after save)

/**
 * Approve an already-saved field.
 * Only valid after saveField succeeded for that field.
 *
 * Backend endpoint: POST /review/:fileId/:formId/field/approval
 *
 * Backend will reject if:
 *   - value not yet saved (APPROVAL_REQUIRES_SAVED_VALUE)
 *   - value is partial / has FORMAT_INVALID flag
 *   - a blocking issue is open on the form
 */
export async function approveField(
  fileId: string,
  formId: string,
  examinationId: string,
  commitmentId: string | null,
  fieldName: FieldName,
  approvalStatus: ApprovalStatus,
  normalized: string,
  version: number,
  targetDetails: { index?: number; expected_value?: string } = {}
): Promise<{ approved: boolean; approval_status: ApprovalStatus; version: number }> {
  if (approvalStatus !== "HUMAN_APPROVED") {
    throw new ApiError(422, { error: "CONFIRMED_ABSENT_UNSUPPORTED" }, "Absence confirmation is not supported by the current API");
  }
  if (USE_MOCK) {
    const result = await mockApproveField(
      fileId, formId, examinationId, commitmentId,
      fieldName, approvalStatus, normalized, version, targetDetails
    );
    return { ...result, version: version + 1 };
  }

  const raw = await request<{ approved: boolean; field: { version: number } | null }>(
    "POST",
    `/review/${fileId}/${formId}/field/approval`,
    {
      target: buildTarget(fieldName, commitmentId, targetDetails),
      action: "APPROVE",
      expected_version: version,
    }
  );
  void examinationId;
  void normalized;
  return { approved: raw.approved, approval_status: approvalStatus, version: raw.field?.version ?? version };
}

// ----------------------------------------------------------------- helpers

function buildTarget(
  fieldName: FieldName,
  commitmentId: string | null,
  details: { index?: number; expected_value?: string } = {}
): Record<string, unknown> {
  const target: Record<string, unknown> = { field: fieldName };
  if (fieldName === "commitment_number") target.commitment_id = commitmentId;
  if (fieldName === "central_numbers") Object.assign(target, details);
  return target;
}

// ----------------------------------------------------------------- export / download

/** GET /documents/:fileId/export/summary */
export async function getExportSummary(fileId: string): Promise<ExportSummary> {
  if (USE_MOCK) return mockGetExportSummary(fileId);
  return request("GET", `/documents/${fileId}/export/summary`);
}

/**
 * GET /documents/:fileId/export.xlsx
 * Returns a Blob; caller triggers download.
 */
export async function downloadExcel(
  fileId: string,
  confirmPartial = false
): Promise<Blob> {
  if (USE_MOCK) {
    const summary = await mockGetExportSummary(fileId);
    if (summary.is_partial && !confirmPartial) {
      throw new ApiError(409, { detail: { needs_confirmation: true, summary } }, "Partial export needs confirmation");
    }
    return mockDownloadExcel(fileId);
  }
  const generation = authSessionGeneration;
  const url = `${BASE_URL}/documents/${fileId}/export.xlsx${confirmPartial ? "?confirm_partial=true" : ""}`;
  const res = await fetchInAuthSession(url, { credentials: "include" });
  if (!isAuthSessionCurrent(generation)) {
    throw new DOMException("Authentication session changed", "AbortError");
  }
  if (!res.ok) {
    let body: unknown;
    try { body = await res.json(); } catch { body = await res.text(); }
    if (!isAuthSessionCurrent(generation)) {
      throw new DOMException("Authentication session changed", "AbortError");
    }
    if (res.status === 401) window.dispatchEvent(new Event("labextract:unauthorized"));
    throw new ApiError(res.status, body, `Excel download failed: HTTP ${res.status}`);
  }
  const blob = await res.blob();
  if (!isAuthSessionCurrent(generation)) {
    throw new DOMException("Authentication session changed", "AbortError");
  }
  return blob;
}
