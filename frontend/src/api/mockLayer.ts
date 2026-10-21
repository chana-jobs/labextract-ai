/**
 * LabExtract AI — Mock intercept layer
 * Isolated from the real API client so it can be tree-shaken in production.
 *
 * Import ONLY from api/client.ts — never import this file directly.
 * All data returned is SYNTHETIC — no real patient data.
 *
 * Two separate mock operations for save and approve reflect the real backend
 * (review.py):
 *   mockSaveField  — saves a value, leaves approval_status = PENDING
 *   mockApproveField — approves an already-saved field (HUMAN_APPROVED /
 *                      CONFIRMED_ABSENT); rejected if no saved value exists
 */

import type {
  ApprovalStatus,
  DocumentMeta,
  EngineResult,
  ExportSummary,
  FieldName,
  FieldValue,
} from "../types/contracts";

import {
  MOCK_DOCUMENTS,
  MOCK_ENGINE_RESULTS,
  MOCK_EXPORT_SUMMARIES,
} from "../mock/syntheticForms";

import { ApiError } from "./errors";

interface MockWorkspace {
  documents: DocumentMeta[];
  engineResults: typeof MOCK_ENGINE_RESULTS;
  exportSummaries: typeof MOCK_EXPORT_SUMMARIES;
}

let mockSession: { user_id: string; workspace_id: string } | null = null;
let mockWorkspace: MockWorkspace | null = null;

function requireMockWorkspace(): MockWorkspace {
  if (!mockSession || !mockWorkspace) {
    throw new ApiError(401, { error: "UNAUTHENTICATED" }, "Mock session not found");
  }
  return mockWorkspace;
}

function assertCurrentMockWorkspace(workspace: MockWorkspace): void {
  if (!mockSession || mockWorkspace !== workspace) {
    throw new ApiError(401, { error: "UNAUTHENTICATED" }, "Mock session changed");
  }
}

function delay(ms = 350): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function findMockField(
  result: EngineResult,
  formId: string,
  examinationId: string,
  commitmentId: string | null,
  fieldName: FieldName,
  target: { index?: number; expected_value?: string }
): FieldValue | undefined {
  const form = (result.forms ?? [result.form]).find((item) => item.form_id === formId);
  const exam = form?.examinations.find((item) => item.examination_id === examinationId);
  if (!exam) return undefined;
  if (fieldName === "commitment_number") {
    return exam.commitments.find((item) => item.commitment_id === commitmentId)?.number;
  }
  if (fieldName === "central_numbers") {
    const value = exam.central_numbers[target.index ?? -1];
    if (!value || (value.normalized ?? value.raw ?? "") !== target.expected_value) return undefined;
    return value;
  }
  return exam[fieldName];
}

// ----------------------------------------------------------------- document list

export async function mockListDocuments(): Promise<DocumentMeta[]> {
  const workspace = requireMockWorkspace();
  await delay();
  assertCurrentMockWorkspace(workspace);
  return structuredClone(workspace.documents);
}

export async function mockGetDocument(fileId: string): Promise<DocumentMeta> {
  const workspace = requireMockWorkspace();
  await delay();
  assertCurrentMockWorkspace(workspace);
  const doc = workspace.documents.find((d) => d.file_id === fileId);
  if (!doc) throw new ApiError(404, null, `Mock: file_id ${fileId} not found`);
  return structuredClone(doc);
}

export async function mockLogin(email: string, password: string): Promise<{ user_id: string; workspace_id: string }> {
  await delay();
  if (!email.trim() || !password) throw new ApiError(401, { error: "INVALID_CREDENTIALS" }, "Mock sign-in failed");
  const user_id = `mock-${crypto.randomUUID()}`;
  const workspace_id = `mock-workspace-${crypto.randomUUID()}`;
  mockWorkspace = {
    documents: structuredClone(MOCK_DOCUMENTS),
    engineResults: structuredClone(MOCK_ENGINE_RESULTS),
    exportSummaries: structuredClone(MOCK_EXPORT_SUMMARIES),
  };
  mockSession = { user_id, workspace_id };
  return { ...mockSession };
}

export async function mockGetSession(): Promise<{ user_id: string; workspace_id: string }> {
  await delay(100);
  if (!mockSession) throw new ApiError(401, { error: "UNAUTHENTICATED" }, "Mock session not found");
  return { ...mockSession };
}

export async function mockLogout(): Promise<void> {
  mockSession = null;
  mockWorkspace = null;
  await delay(100);
}

export async function mockUploadDocument(file: File): Promise<DocumentMeta> {
  const workspace = requireMockWorkspace();
  await delay(800);
  assertCurrentMockWorkspace(workspace);
  const newDoc: DocumentMeta = {
    file_id: `file-upload-${Date.now()}`,
    filename: file.name,
    processing_status: "DONE",
    uploaded_at: new Date().toISOString(),
    page_count: 1,
  };
  workspace.documents.push(newDoc);

  // Clone Form A as the extracted result for the "uploaded" document
  const cloned = structuredClone(workspace.engineResults["file-syn-aaa"]!) as EngineResult;
  cloned.form.file_id = newDoc.file_id;
  cloned.form.form_id = `form-upload-${Date.now()}`;
  workspace.engineResults[newDoc.file_id] = cloned;

  workspace.exportSummaries[newDoc.file_id] = {
    file_id: newDoc.file_id,
    rows: 2,
    ready: 0,
    closed_with_gaps: 0,
    pending_rows: 2,
    orange_cells: 6,
    uncounted_forms: 0,
    failed_forms: 0,
    forms_without_commitments: 0,
    filename: "labextract.xlsx",
    is_partial: false,
    warnings: ["[MOCK] נתונים סינתטיים — לא תוצאות OCR אמיתיות"],
  };
  return structuredClone(newDoc);
}

export async function mockInitDrafts(_fileId: string): Promise<void> {
  const workspace = requireMockWorkspace();
  await delay(500);
  assertCurrentMockWorkspace(workspace);
}

export async function mockGetDrafts(fileId: string): Promise<EngineResult> {
  const workspace = requireMockWorkspace();
  await delay();
  assertCurrentMockWorkspace(workspace);
  const result = workspace.engineResults[fileId];
  if (!result)
    throw new ApiError(404, null, `Mock: no drafts for file_id ${fileId}`);
  return structuredClone(result);
}

// ----------------------------------------------------------------- save (PENDING stays)

/**
 * Simulates PATCH /review/:fileId/:formId/field with a value update only.
 * Sets approval_status = PENDING (never approves).
 * Returns the updated normalized value for the caller to apply.
 */
export async function mockSaveField(
  fileId: string,
  formId: string,
  examinationId: string,
  commitmentId: string | null,
  fieldName: FieldName,
  normalized: string,
  version: number,
  target: { index?: number; expected_value?: string } = {}
): Promise<{ saved: boolean; normalized: string; version: number }> {
  const workspace = requireMockWorkspace();
  await delay(400);
  assertCurrentMockWorkspace(workspace);

  // Validate version against in-memory store to simulate optimistic-lock
  const engineResult = workspace.engineResults[fileId];
  if (!engineResult) {
    throw new ApiError(404, null, "Mock: form not found");
  }
  const field = findMockField(engineResult, formId, examinationId, commitmentId, fieldName, target);
  if (!field) throw new ApiError(404, { error: "TARGET_NOT_FOUND" }, "Mock: target not found");
  if (field.version !== version) {
    throw new ApiError(409, { error: "VERSION_CONFLICT", server_version: field.version }, "Mock: version conflict");
  }

  if (fieldName === "sample_received_date" && normalized && !isValidMockDate(normalized)) {
    throw new ApiError(422, { error: "DATE_FORMAT_INVALID", expected: "YYYY-MM-DD" }, "Mock: invalid date");
  }
  if (field.normalized === normalized) {
    return { saved: true, normalized, version: field.version };
  }

  field.raw = normalized || null;
  field.normalized = normalized && !normalized.includes("?") &&
    (fieldName !== "israeli_id" || /^\d{9}$/.test(normalized)) ? normalized : null;
  field.partial = normalized.includes("?");
  field.unknown_positions = [...normalized].flatMap((character, index) => character === "?" ? [index] : []);
  field.extraction_status = field.partial
    ? "PARTIAL"
    : field.normalized
      ? "COMPLETE"
      : "UNCERTAIN";
  field.flags = fieldName === "israeli_id" && normalized && !/^\d{9}$/.test(normalized)
    ? ["FORMAT_INVALID"]
    : [];
  field.approval_status = "PENDING";
  field.version += 1;
  return { saved: true, normalized, version: field.version };
}

function isValidMockDate(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return false;
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return date.getUTCFullYear() === Number(match[1]) &&
    date.getUTCMonth() === Number(match[2]) - 1 &&
    date.getUTCDate() === Number(match[3]);
}

// ----------------------------------------------------------------- approve (after save)

/**
 * Simulates approval of a field that already has a saved value.
 * Refuses if the field is still MISSING and has no saved value (not approvable).
 */
export async function mockApproveField(
  fileId: string,
  formId: string,
  examinationId: string,
  commitmentId: string | null,
  fieldName: FieldName,
  approvalStatus: ApprovalStatus,
  normalized: string,
  version: number,
  target: { index?: number; expected_value?: string } = {}
): Promise<{ approved: boolean; approval_status: ApprovalStatus }> {
  const workspace = requireMockWorkspace();
  await delay(300);
  assertCurrentMockWorkspace(workspace);

  const engineResult = workspace.engineResults[fileId];
  if (!engineResult) {
    throw new ApiError(404, null, "Mock: form not found");
  }
  if (approvalStatus !== "HUMAN_APPROVED") {
    throw new ApiError(422, { error: "CONFIRMED_ABSENT_UNSUPPORTED" }, "Mock absence confirmation is unsupported");
  }
  const field = findMockField(engineResult, formId, examinationId, commitmentId, fieldName, target);
  if (!field) throw new ApiError(404, { error: "TARGET_NOT_FOUND" }, "Mock: target not found");
  if (field.version !== version) {
    throw new ApiError(409, { error: "VERSION_CONFLICT", server_version: field.version }, "Mock: version conflict");
  }
  if (!normalized || field.extraction_status === "MISSING" || field.partial || field.flags.includes("FORMAT_INVALID")) {
    throw new ApiError(422, { error: "NOT_APPROVABLE" }, "Mock: field is not approvable");
  }
  field.approval_status = "HUMAN_APPROVED";
  field.version += 1;
  return { approved: true, approval_status: approvalStatus };
}

// ----------------------------------------------------------------- legacy combined (mock-only, for backward compat)

/** @deprecated Use mockSaveField + mockApproveField separately */
export async function mockSaveReview(
  fileId: string,
  formId: string,
  payload: unknown
): Promise<void> {
  await delay(400);
  console.info("[mock] saveReview (legacy combined)", { fileId, formId, payload });
}

// ----------------------------------------------------------------- export / download

export async function mockGetExportSummary(
  fileId: string
): Promise<ExportSummary> {
  const workspace = requireMockWorkspace();
  await delay();
  assertCurrentMockWorkspace(workspace);
  const summary = workspace.exportSummaries[fileId];
  if (!summary)
    throw new ApiError(404, null, `Mock: no summary for file_id ${fileId}`);
  return structuredClone(summary);
}

export async function mockDownloadExcel(_fileId: string): Promise<Blob> {
  const workspace = requireMockWorkspace();
  await delay(600);
  assertCurrentMockWorkspace(workspace);
  return new Blob(["[MOCK EXCEL — synthetic data only]"], {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
}
