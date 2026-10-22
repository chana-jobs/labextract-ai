/**
 * ReviewWorkspaceV2 — data model, row status and the shared selection rule.
 *
 * Status: the server persists approval_status per field but does not return a
 * commitment status. deriveCommitmentStatus mirrors shared/contracts.py
 * derive_commitment_status exactly; rowStatus then adds conservative demotions
 * (open issues, uncertain existence/association) so the UI never shows a row as
 * ready when the server would not. The demotions are those of backend/readiness.py
 * commitment_status (open form/row issue, uncertain existence or association); no
 * API returns that status per commitment yet, so it is recomputed here.
 * Nothing here counts populated cells.
 *
 * Selection: the active tab + every filter define ONE set of rows/forms. The
 * grid, Excel and source-form actions all read that same set (never one page).
 */

import type {
  Commitment,
  CommitmentStatus,
  DocumentMeta,
  Examination,
  FieldName,
  FieldValue,
  Form,
  IssueReason,
} from "../types/contracts";

// ----------------------------------------------------------------- documents

export type DocLoad =
  | { state: "loading" }
  | { state: "pending" }            // QUEUED/RUNNING or drafts not initialised
  | { state: "processing" }         // explicit init in flight
  | { state: "ready"; forms: Form[] }
  | { state: "error"; message: string };

export interface DocEntry {
  meta: DocumentMeta;
  load: DocLoad;
}

export function uploadedAt(meta: DocumentMeta): string {
  return meta.uploaded_at ?? meta.upload_date ?? "";
}

/** Local calendar date (YYYY-MM-DD) of an ISO timestamp, or "" if invalid. */
export function localDate(iso: string): string {
  const date = new Date(iso);
  if (!iso || Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

// ----------------------------------------------------------------- status

/** Mirrors backend/review.py BLOCKING_ISSUES. */
const BLOCKING_ISSUES: IssueReason[] = [
  "REVIEW_RELINK_REQUIRED",
  "UNREADABLE_DOCUMENT",
  "SPLIT_SUSPECT",
  "TECHNICAL_FAILURE",
];

const ISSUE_LABELS: Record<IssueReason, string> = {
  COMMITMENT_COUNT_UNCERTAIN: "מספר ההתחייבויות בטופס אינו ודאי",
  REVIEW_RELINK_REQUIRED: "נדרש שיוך מחדש",
  UNREADABLE_DOCUMENT: "מסמך לא קריא",
  SPLIT_SUSPECT: "חשד לפיצול שגוי",
  TECHNICAL_FAILURE: "כשל טכני בעיבוד",
};

export function issueLabel(reason: IssueReason): string {
  return ISSUE_LABELS[reason] ?? reason;
}

function isApprovedWithValue(f: FieldValue): boolean {
  return (f.approval_status === "AUTO_APPROVED" || f.approval_status === "HUMAN_APPROVED") && !!f.normalized;
}

export function rowFields(exam: Examination, c: Commitment): FieldValue[] {
  return [
    exam.first_name, exam.last_name, exam.israeli_id,
    exam.sample_received_date, exam.sender, c.number,
    ...exam.central_numbers,
  ];
}

/** Exact mirror of shared/contracts.py derive_commitment_status. */
export function deriveCommitmentStatus(exam: Examination, c: Commitment, formFailed: boolean): CommitmentStatus {
  if (formFailed) return "FAILED";
  const fields = [exam.first_name, exam.last_name, exam.israeli_id, exam.sample_received_date, exam.sender, c.number];
  if (exam.central_numbers.length === 0) return "NEEDS_REVIEW";
  fields.push(...exam.central_numbers);
  if (fields.some((f) => f.resolution_state === "AWAITING_EXTERNAL")) return "AWAITING_EXTERNAL";
  if (fields.every((f) => isApprovedWithValue(f) || f.approval_status === "CONFIRMED_ABSENT")) {
    return fields.some((f) => f.approval_status === "CONFIRMED_ABSENT") ? "READY_WITH_CONFIRMED_GAPS" : "READY";
  }
  return "NEEDS_REVIEW";
}

export type TabId = "complete" | "review" | "gaps" | "all" | "forms";

export interface RowStatus {
  view: "complete" | "review" | "gaps";
  derived: CommitmentStatus;
  /** Complete rows only: every field AUTO_APPROVED by the engine vs. completed by a person. */
  origin: "auto" | "human" | null;
  reasons: string[];
}

export function rowStatus(form: Form, exam: Examination, c: Commitment): RowStatus {
  const derived = deriveCommitmentStatus(exam, c, form.processing_status === "FAILED");
  const reasons: string[] = [];

  for (const issue of form.issues) {
    if (issue.resolution_state === "CLOSED") continue;
    if (issue.commitment_id !== null && issue.commitment_id !== c.commitment_id) continue;
    // Any open form-level or row-linked issue keeps the row out of "ready", including an open
    // count issue: the form is not closed. Only BLOCKING_ISSUES also block approval (server rule).
    reasons.push(BLOCKING_ISSUES.includes(issue.reason) ? `בעיה חוסמת: ${issueLabel(issue.reason)}` : `הטופס לא נסגר: ${issueLabel(issue.reason)}`);
  }
  if (c.existence_status === "UNCERTAIN") reasons.push("קיום ההתחייבות אינו ודאי");
  if (exam.association_status === "ASSOCIATION_UNCERTAIN") reasons.push("שיוך ההתחייבות לבדיקה אינו ודאי");

  if (derived === "FAILED") reasons.unshift("הטופס נכשל בעיבוד");
  else if (derived === "AWAITING_EXTERNAL") reasons.unshift("ממתין לאימות חיצוני");
  else if (derived === "NEEDS_REVIEW") {
    if (exam.central_numbers.length === 0) reasons.unshift("לא זוהו מספרים מרכזיים");
    const pending = rowFields(exam, c).filter((f) => !isApprovedWithValue(f) && f.approval_status !== "CONFIRMED_ABSENT").length;
    if (pending > 0) reasons.unshift(`${pending} שדות לא מאושרים`);
  }

  const closed = derived === "READY" || derived === "READY_WITH_CONFIRMED_GAPS";
  if (!closed || reasons.length > 0) return { view: "review", derived, origin: null, reasons };
  const auto = rowFields(exam, c).every((f) => f.approval_status === "AUTO_APPROVED");
  return {
    view: derived === "READY" ? "complete" : "gaps",
    derived,
    origin: auto ? "auto" : "human",
    reasons,
  };
}

// ----------------------------------------------------------------- rows and forms

export interface CommitmentRowV2 {
  key: string;
  doc: DocumentMeta;
  form: Form;
  exam: Examination;
  commitment: Commitment;
  status: RowStatus;
}

export interface FormRecord {
  key: string;
  doc: DocumentMeta;
  form: Form;
  /** First page (1-based) recorded by the server for this form, if any. */
  page: number | null;
  reasons: string[];
}

export function formPage(form: Form): number | null {
  const region = form.page_regions.find((r) => typeof r.page === "number");
  return typeof region?.page === "number" ? region.page + 1 : null;
}

export function buildRows(docs: DocEntry[]): CommitmentRowV2[] {
  const rows: CommitmentRowV2[] = [];
  for (const { meta, load } of docs) {
    if (load.state !== "ready") continue;
    for (const form of load.forms) {
      for (const exam of form.examinations) {
        for (const commitment of exam.commitments) {
          rows.push({
            key: `${meta.file_id}:${form.form_id}:${commitment.commitment_id}`,
            doc: meta, form, exam, commitment,
            status: rowStatus(form, exam, commitment),
          });
        }
      }
    }
  }
  return rows;
}

/** Forms the server returned that have no commitments, failed, or carry open form-level/count issues. */
export function buildExceptionForms(docs: DocEntry[]): FormRecord[] {
  const records: FormRecord[] = [];
  for (const { meta, load } of docs) {
    if (load.state !== "ready") continue;
    for (const form of load.forms) {
      const reasons: string[] = [];
      // A technical failure is not an empty form: say so instead of "no commitments extracted".
      if (form.processing_status === "FAILED") reasons.push("כשל טכני בעיבוד — יש להזין ערכים מהמקור");
      else if (!form.examinations.some((e) => e.commitments.length > 0)) reasons.push("לא חולצו התחייבויות");
      for (const issue of form.issues) {
        if (issue.resolution_state === "CLOSED") continue;
        if (issue.commitment_id === null || issue.reason === "COMMITMENT_COUNT_UNCERTAIN") {
          reasons.push(issue.note ? `${issueLabel(issue.reason)}: ${issue.note}` : issueLabel(issue.reason));
        }
      }
      if (reasons.length > 0) {
        records.push({ key: `${meta.file_id}:${form.form_id}`, doc: meta, form, page: formPage(form), reasons });
      }
    }
  }
  return records;
}

// ----------------------------------------------------------------- filters and selection

export type Scope = "latest_file" | "upload_range" | "all";

export interface Filters {
  scope: Scope;
  uploadFrom: string;
  uploadTo: string;
  query: string;
  sender: string;
  receiptFrom: string;
  receiptTo: string;
}

export const EMPTY_FILTERS: Filters = {
  scope: "latest_file",
  uploadFrom: "", uploadTo: "",
  query: "", sender: "",
  receiptFrom: "", receiptTo: "",
};

export function latestDoc(docs: DocEntry[]): DocEntry | undefined {
  return docs.reduce<DocEntry | undefined>(
    (best, d) => (!best || uploadedAt(d.meta) > uploadedAt(best.meta) ? d : best),
    undefined
  );
}

export function docsInScope(docs: DocEntry[], f: Filters): DocEntry[] {
  if (f.scope === "latest_file") {
    const latest = latestDoc(docs);
    return latest ? [latest] : [];
  }
  if (f.scope === "upload_range") {
    return docs.filter((d) => {
      const day = localDate(uploadedAt(d.meta));
      if (!day) return false;
      return (!f.uploadFrom || day >= f.uploadFrom) && (!f.uploadTo || day <= f.uploadTo);
    });
  }
  return docs;
}

export function fieldText(f: FieldValue): string {
  return f.normalized ?? f.raw ?? "";
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Row-level filters (query, sender, receipt date). Identifiers compared as strings: leading zeros kept. */
export function rowMatches(row: CommitmentRowV2, f: Filters): boolean {
  const q = f.query.trim().toLowerCase();
  if (q) {
    const { exam, commitment } = row;
    const haystack = [
      exam.first_name.normalized, exam.first_name.raw,
      exam.last_name.normalized, exam.last_name.raw,
      exam.israeli_id.normalized, exam.israeli_id.raw,
      commitment.number.normalized, commitment.number.raw,
      ...exam.central_numbers.flatMap((c) => [c.normalized, c.raw]),
    ].filter(Boolean).join(" ").toLowerCase();
    if (!haystack.includes(q)) return false;
  }
  if (f.sender && fieldText(row.exam.sender) !== f.sender) return false;
  if (f.receiptFrom || f.receiptTo) {
    const day = row.exam.sample_received_date.normalized ?? "";
    if (!ISO_DATE.test(day)) return false;
    if (f.receiptFrom && day < f.receiptFrom) return false;
    if (f.receiptTo && day > f.receiptTo) return false;
  }
  return true;
}

export function rowInTab(row: CommitmentRowV2, tab: TabId): boolean {
  if (tab === "all") return true;
  if (tab === "forms") return false;
  return row.status.view === tab;
}

function formMatches(record: FormRecord, f: Filters): boolean {
  const q = f.query.trim().toLowerCase();
  if (!q) return true;
  return [record.doc.filename, record.form.form_id, ...record.reasons].join(" ").toLowerCase().includes(q);
}

export interface Selection {
  scopeDocs: DocEntry[];
  /** Rows in scope matching every row filter, before the tab is applied. */
  filteredRows: CommitmentRowV2[];
  rows: CommitmentRowV2[];
  forms: FormRecord[];
  counts: Record<TabId, number>;
}

export function select(docs: DocEntry[], tab: TabId, f: Filters): Selection {
  const scopeDocs = docsInScope(docs, f);
  const filteredRows = buildRows(scopeDocs).filter((r) => rowMatches(r, f));
  const forms = buildExceptionForms(scopeDocs).filter((r) => formMatches(r, f));
  const counts: Record<TabId, number> = {
    complete: filteredRows.filter((r) => r.status.view === "complete").length,
    review: filteredRows.filter((r) => r.status.view === "review").length,
    gaps: filteredRows.filter((r) => r.status.view === "gaps").length,
    all: filteredRows.length,
    forms: forms.length,
  };
  return {
    scopeDocs,
    filteredRows,
    rows: filteredRows.filter((r) => rowInTab(r, tab)),
    forms: tab === "forms" ? forms : [],
    counts,
  };
}

// ----------------------------------------------------------------- export / source availability

export type Availability = { ok: true; fileId: string; filename: string; rows: number } | { ok: false; reason: string };

function sameKeys(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const set = new Set(a);
  return b.every((k) => set.has(k));
}

/**
 * The server exports one whole file (non-failed forms only). Excel is offered
 * only when the selected set is exactly that file's export set.
 */
export function excelAvailability(docs: DocEntry[], tab: TabId, sel: Selection): Availability {
  if (tab === "forms") return { ok: false, reason: "דוח חריגים לטפסים דורש endpoint ייעודי בשרת (Backend)." };
  if (sel.rows.length === 0) return { ok: false, reason: "אין התחייבויות בבחירה הנוכחית." };
  const fileIds = new Set(sel.rows.map((r) => r.doc.file_id));
  if (fileIds.size > 1) {
    return { ok: false, reason: `הבחירה כוללת ${fileIds.size} קבצים. השרת תומך בייצוא של קובץ בודד בלבד; ייצוא לפי סינון דורש Backend.` };
  }
  const entry = docs.find((d) => d.meta.file_id === sel.rows[0]!.doc.file_id);
  if (!entry || entry.load.state !== "ready") return { ok: false, reason: "נתוני הקובץ לא נטענו." };
  const exportKeys = buildRows([entry]).filter((r) => r.form.processing_status !== "FAILED").map((r) => r.key);
  if (!sameKeys(exportKeys, sel.rows.map((r) => r.key))) {
    return {
      ok: false,
      reason: `הבחירה כוללת ${sel.rows.length} מתוך ${exportKeys.length} התחייבויות בקובץ. השרת מייצא קובץ שלם בלבד; ייצוא חלקי לפי סינון דורש Backend.`,
    };
  }
  return { ok: true, fileId: entry.meta.file_id, filename: entry.meta.filename, rows: exportKeys.length };
}

/** Unique source forms of the selection (a form with several commitments appears once). */
export function selectedSourceForms(tab: TabId, sel: Selection): FormRecord[] {
  if (tab === "forms") return sel.forms;
  const seen = new Map<string, FormRecord>();
  for (const row of sel.rows) {
    const key = `${row.doc.file_id}:${row.form.form_id}`;
    if (!seen.has(key)) seen.set(key, { key, doc: row.doc, form: row.form, page: formPage(row.form), reasons: [] });
  }
  return [...seen.values()];
}

/**
 * The server serves only the whole original file. Download is allowed only if
 * that file holds nothing outside the selection: every form of the file, and
 * (for commitment tabs) every commitment of it, is selected.
 */
export function sourceDownloadAvailability(docs: DocEntry[], tab: TabId, sel: Selection): Availability {
  const forms = selectedSourceForms(tab, sel);
  if (forms.length === 0) return { ok: false, reason: "אין טפסים בבחירה הנוכחית." };
  const fileIds = new Set(forms.map((f) => f.doc.file_id));
  if (fileIds.size > 1) {
    return { ok: false, reason: `הבחירה כוללת ${fileIds.size} קבצים. הורדה מרוכזת (ZIP) דורשת Backend.` };
  }
  const entry = docs.find((d) => d.meta.file_id === forms[0]!.doc.file_id);
  if (!entry || entry.load.state !== "ready") return { ok: false, reason: "נתוני הקובץ לא נטענו." };
  const allForms = entry.load.forms.map((f) => `${entry.meta.file_id}:${f.form_id}`);
  if (!sameKeys(allForms, forms.map((f) => f.key))) {
    return {
      ok: false,
      reason: `הקובץ המקורי מכיל ${allForms.length} טפסים ונבחרו ${forms.length}. הורדת טפסים נבחרים בלבד דורשת Backend.`,
    };
  }
  if (tab !== "forms") {
    const allRows = buildRows([entry]).map((r) => r.key);
    if (!sameKeys(allRows, sel.rows.map((r) => r.key))) {
      return { ok: false, reason: "הקובץ המקורי כולל התחייבויות שאינן בבחירה. הורדה מבודדת דורשת Backend." };
    }
  }
  return { ok: true, fileId: entry.meta.file_id, filename: entry.meta.filename, rows: forms.length };
}

export type CellKey = string;

export function cellKey(rowKey: string, field: FieldName, index?: number): CellKey {
  return `${rowKey}|${field}|${index ?? ""}`;
}
