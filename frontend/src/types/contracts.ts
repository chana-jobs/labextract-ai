/**
 * LabExtract AI — Frontend type definitions
 * Mirrors shared/contracts.py (v0.1) for TypeScript.
 * Source of truth: shared/contracts.py + Spec v3.2.
 * All data here is SYNTHETIC — no real patient data.
 */

// ----------------------------------------------------------------- enums

export type ExtractionStatus =
  | "COMPLETE"
  | "PARTIAL"
  | "UNCERTAIN"
  | "CONFLICT"
  | "MISSING";

export type ApprovalStatus =
  | "PENDING"
  | "AUTO_APPROVED"
  | "HUMAN_APPROVED"
  | "CONFIRMED_ABSENT";

export type ValueSource = "DOCUMENT" | "EXTERNAL_VERIFICATION";

export type ResolutionState = "OPEN" | "AWAITING_EXTERNAL" | "CLOSED";

export type ProcessingStatus = "QUEUED" | "RUNNING" | "DONE" | "FAILED";

export type SplitStatus = "CONFIRMED" | "UNCERTAIN" | "MANUALLY_CONFIRMED";

export type CommitmentStatus =
  | "READY"
  | "READY_WITH_CONFIRMED_GAPS"
  | "NEEDS_REVIEW"
  | "AWAITING_EXTERNAL"
  | "FAILED";

export type ExistenceStatus = "CONFIRMED" | "UNCERTAIN";

export type AssociationStatus =
  | "DEFAULT_EXAM_LEVEL"
  | "ASSOCIATION_UNCERTAIN";

export type Readability = "OK" | "DOUBT" | "POOR";

export type Attribution = "STRONG" | "MEDIUM" | "WEAK";

export type Verification = "INDEPENDENT" | "AGREEMENT" | "NONE";

export type UncertaintyReason =
  | "RECOGNITION"
  | "ATTRIBUTION"
  | "CANDIDATE_CONFLICT"
  | "UNGROUNDED";

export type IssueReason =
  | "COMMITMENT_COUNT_UNCERTAIN"
  | "REVIEW_RELINK_REQUIRED"
  | "UNREADABLE_DOCUMENT"
  | "SPLIT_SUSPECT"
  | "TECHNICAL_FAILURE";

export type EngineMode = "FULL" | "LOCAL_ONLY";

export type FieldName =
  | "first_name"
  | "last_name"
  | "israeli_id"
  | "central_numbers"
  | "sample_received_date"
  | "sender"
  | "commitment_number";

// ----------------------------------------------------------------- building blocks

export interface Evidence {
  page: number;
  bbox: [number, number, number, number] | null; // [x0, y0, x1, y1] in source coords; null = no reliable position
  region_id: string | null;
  method: string;
  engine_version: string;
  role_basis: string | null;
  tier: Attribution | null;
}

export interface Candidate {
  raw: string;
  normalized: string | null;
  tier: Attribution | null;
  role_basis: string | null;
  evidence: Evidence[];
}

export interface Dimensions {
  readability: Readability;
  attribution: Attribution;
  verification: Verification;
}

export interface FieldValue {
  field_name: FieldName;
  raw: string | null;
  normalized: string | null;
  partial: boolean;
  unknown_positions: number[];

  extraction_status: ExtractionStatus;
  approval_status: ApprovalStatus;
  value_source: ValueSource;
  resolution_state: ResolutionState;

  dimensions: Dimensions | null;
  uncertainty_reason: UncertaintyReason[];
  flags: string[];
  candidates: Candidate[];
  evidence: Evidence[];

  auto_approval_rule_id: string | null;
  policy_version: string | null;
  /** Optimistic-locking counter */
  version: number;
}

// ----------------------------------------------------------------- entities

export interface Commitment {
  commitment_id: string;
  examination_id: string;
  existence_status: ExistenceStatus;
  existence_evidence_ref: string | null;
  origin_anchor: string | null;
  internal_index: number;
  number: FieldValue; // field_name === "commitment_number"
}

export interface Examination {
  examination_id: string;
  form_id: string;
  association_status: AssociationStatus;
  first_name: FieldValue;
  last_name: FieldValue;
  israeli_id: FieldValue;
  sample_received_date: FieldValue;
  sender: FieldValue;
  central_numbers: FieldValue[];
  commitments: Commitment[];
}

export interface Issue {
  issue_id: string;
  form_id: string;
  commitment_id: string | null;
  reason: IssueReason;
  resolution_state: ResolutionState;
  note: string | null;
}

export interface Form {
  form_id: string;
  file_id: string;
  page_regions: Record<string, unknown>[];
  split_status: SplitStatus;
  split_revision: number;
  processing_status: ProcessingStatus;
  examinations: Examination[];
  issues: Issue[];
}

export interface EngineRun {
  run_id: string;
  engine_version: string;
  mode: EngineMode;
}

/** What the extraction engine returns for ONE form. */
export interface EngineResult {
  engine_run: EngineRun;
  form: Form;
  forms?: Form[];
}

// ----------------------------------------------------------------- API response shapes

export interface DocumentMeta {
  file_id: string;
  filename: string;
  processing_status: ProcessingStatus;
  uploaded_at?: string; // ISO 8601, backend response
  upload_date?: string; // mock compatibility
  page_count: number | null;
  split_status?: SplitStatus;
}

export interface ExportSummary {
  file_id?: string;
  rows: number;
  ready: number;
  closed_with_gaps: number;
  pending_rows: number;
  orange_cells: number;
  uncounted_forms: number;
  failed_forms: number;
  forms_without_commitments: number;
  filename: string;
  is_partial: boolean;
  warnings: string[];
}

// ----------------------------------------------------------------- review save payload

export interface ReviewSavePayload {
  form_id: string;
  file_id: string;
  /** Partial update: only the fields the user touched */
  field_updates: FieldUpdateItem[];
  record_version: number;
}

export interface FieldUpdateItem {
  examination_id: string;
  commitment_id: string | null; // null = examination-level field
  field_name: FieldName;
  normalized: string;
  approval_status: ApprovalStatus;
  version: number;
}
