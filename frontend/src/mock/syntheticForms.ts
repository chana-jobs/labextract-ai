/**
 * LabExtract AI — Synthetic mock data
 * ALL data here is INVENTED for testing purposes only.
 * No real patient names, IDs or medical details are used.
 * Required by Spec v3.2 §2.2 (competition/demo build uses synthetic data only).
 *
 * ⚠️  THIS FILE IS LABELED AS MOCK — never present as real OCR output.
 */

import type {
  DocumentMeta,
  EngineResult,
  ExportSummary,
  FieldValue,
  Form,
} from "../types/contracts";

// ----------------------------------------------------------------- helpers

function pendingField(
  field_name: FieldValue["field_name"],
  raw: string,
  normalized: string
): FieldValue {
  return {
    field_name,
    raw,
    normalized,
    partial: false,
    unknown_positions: [],
    extraction_status: "COMPLETE",
    approval_status: "PENDING",
    value_source: "DOCUMENT",
    resolution_state: "OPEN",
    dimensions: { readability: "OK", attribution: "STRONG", verification: "INDEPENDENT" },
    uncertainty_reason: [],
    flags: [],
    candidates: [],
    evidence: [
      {
        page: 0,
        bbox: [50, 120, 300, 145],
        region_id: "r-001",
        method: "mock",
        engine_version: "mock-0.1",
        role_basis: null,
        tier: "STRONG",
      },
    ],
    auto_approval_rule_id: null,
    policy_version: null,
    version: 1,
  };
}

function missingField(field_name: FieldValue["field_name"]): FieldValue {
  return {
    field_name,
    raw: null,
    normalized: null,
    partial: false,
    unknown_positions: [],
    extraction_status: "MISSING",
    approval_status: "PENDING",
    value_source: "DOCUMENT",
    resolution_state: "OPEN",
    dimensions: null,
    uncertainty_reason: [],
    flags: [],
    candidates: [],
    evidence: [],
    auto_approval_rule_id: null,
    policy_version: null,
    version: 1,
  };
}

// ----------------------------------------------------------------- synthetic forms

/**
 * SYNTHETIC Form A — complete extraction, all fields found.
 * Commitment count: 2.
 */
const MOCK_FORM_A: Form = {
  form_id: "form-syn-001",
  file_id: "file-syn-aaa",
  page_regions: [],
  split_status: "CONFIRMED",
  split_revision: 1,
  processing_status: "DONE",
  examinations: [
    {
      examination_id: "exam-syn-001",
      form_id: "form-syn-001",
      association_status: "DEFAULT_EXAM_LEVEL",
      first_name: pendingField("first_name", "אביגיל", "אביגיל"),
      last_name: pendingField("last_name", "כהן-לוי", "כהן-לוי"),
      israeli_id: pendingField("israeli_id", "012345678", "012345678"),
      sample_received_date: pendingField("sample_received_date", "03/07/2026", "2026-07-03"),
      sender: pendingField("sender", "מכבי בית שמש", "מכבי בית שמש"),
      central_numbers: [
        pendingField("central_numbers", "7001234", "7001234"),
        pendingField("central_numbers", "7001235", "7001235"),
      ],
      commitments: [
        {
          commitment_id: "cmt-syn-001a",
          examination_id: "exam-syn-001",
          existence_status: "CONFIRMED",
          existence_evidence_ref: "r-001",
          origin_anchor: "page:0:region:r-002",
          internal_index: 0,
          number: pendingField("commitment_number", "00421", "00421"),
        },
        {
          commitment_id: "cmt-syn-001b",
          examination_id: "exam-syn-001",
          existence_status: "CONFIRMED",
          existence_evidence_ref: "r-003",
          origin_anchor: "page:0:region:r-004",
          internal_index: 1,
          number: pendingField("commitment_number", "00422", "00422"),
        },
      ],
    },
  ],
  issues: [],
};

/**
 * SYNTHETIC Form B — partial extraction, sender unmapped, commitment number unreadable.
 */
const MOCK_FORM_B: Form = {
  form_id: "form-syn-002",
  file_id: "file-syn-bbb",
  page_regions: [],
  split_status: "UNCERTAIN",
  split_revision: 1,
  processing_status: "DONE",
  examinations: [
    {
      examination_id: "exam-syn-002",
      form_id: "form-syn-002",
      association_status: "DEFAULT_EXAM_LEVEL",
      first_name: pendingField("first_name", "דוד", "דוד"),
      last_name: pendingField("last_name", "לבנון", "לבנון"),
      israeli_id: {
        ...pendingField("israeli_id", "099887766", "099887766"),
        flags: ["ID_CHECKSUM_WARNING"],
      },
      sample_received_date: {
        ...pendingField("sample_received_date", "07/03/2026", "2026-03-07"),
        flags: ["DATE_AMBIGUOUS"],
        uncertainty_reason: ["ATTRIBUTION"],
      },
      sender: {
        ...missingField("sender"),
        raw: "מרפאה לא ידועה",
        flags: ["SENDER_UNMAPPED"],
        extraction_status: "UNCERTAIN",
      },
      central_numbers: [pendingField("central_numbers", "8009991", "8009991")],
      commitments: [
        {
          commitment_id: "cmt-syn-002a",
          examination_id: "exam-syn-002",
          existence_status: "CONFIRMED",
          existence_evidence_ref: "r-010",
          origin_anchor: "page:0:region:r-011",
          internal_index: 0,
          number: {
            ...missingField("commitment_number"),
            extraction_status: "PARTIAL",
            partial: true,
            unknown_positions: [2, 3],
            raw: "0?4?7",
          },
        },
      ],
    },
  ],
  issues: [
    {
      issue_id: "iss-syn-001",
      form_id: "form-syn-002",
      commitment_id: null,
      reason: "COMMITMENT_COUNT_UNCERTAIN",
      resolution_state: "OPEN",
      note: "Synthetic: count of commitments unclear from scan quality.",
    },
  ],
};

// ----------------------------------------------------------------- exported mock engine results

export const MOCK_ENGINE_RESULTS: Record<string, EngineResult> = {
  "file-syn-aaa": {
    engine_run: {
      run_id: "run-syn-001",
      engine_version: "mock-0.1",
      mode: "FULL",
    },
    form: MOCK_FORM_A,
  },
  "file-syn-bbb": {
    engine_run: {
      run_id: "run-syn-002",
      engine_version: "mock-0.1",
      mode: "FULL",
    },
    form: MOCK_FORM_B,
  },
};

// ----------------------------------------------------------------- document list

export const MOCK_DOCUMENTS: DocumentMeta[] = [
  {
    file_id: "file-syn-aaa",
    filename: "synthetic_form_complete.pdf",
    processing_status: "DONE",
    upload_date: "2026-10-19T08:00:00Z",
    page_count: 1,
  },
  {
    file_id: "file-syn-bbb",
    filename: "synthetic_form_partial.pdf",
    processing_status: "DONE",
    upload_date: "2026-10-19T09:30:00Z",
    page_count: 2,
  },
];

// ----------------------------------------------------------------- export summaries

export const MOCK_EXPORT_SUMMARIES: Record<string, ExportSummary> = {
  "file-syn-aaa": {
    file_id: "file-syn-aaa",
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
    warnings: [],
  },
  "file-syn-bbb": {
    file_id: "file-syn-bbb",
    rows: 1,
    ready: 0,
    closed_with_gaps: 0,
    pending_rows: 1,
    orange_cells: 3,
    uncounted_forms: 0,
    failed_forms: 0,
    forms_without_commitments: 0,
    filename: "labextract.xlsx",
    is_partial: true,
    warnings: [
      "SENDER_UNMAPPED: sender field not in vocabulary",
      "DATE_AMBIGUOUS: sample receipt date DD/MM ambiguity",
      "ID_CHECKSUM_WARNING: Israeli ID checksum failed",
    ],
  },
};
