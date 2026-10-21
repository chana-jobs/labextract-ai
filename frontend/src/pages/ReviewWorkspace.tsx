



/**
 * ReviewWorkspace — main review page (Task 2).
 *
 * Save / Approve are SEPARATE operations (Spec §7.2, Backend review.py):
 *   handleFieldSave   → saveField()   → approval_status stays PENDING
 *   handleFieldApprove → approveField() → approval_status = HUMAN_APPROVED
 *                                          or CONFIRMED_ABSENT
 *
 * The reducer applies the state change only AFTER server confirmation (I-3).
 * saveError is kept in state and shown inline without clearing the field (I-4).
 *
 * GET /documents gap: listDocuments() throws in real mode.
 * Documents are tracked in local state; uploads add to that list.
 */

import {
  useEffect,
  useReducer,
  useCallback,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ApiError,
  downloadExcel,
  getAuthSessionGeneration,
  getExportSummary,
  getDocument,
  getDrafts,
  initDrafts,
  isAuthSessionCurrent,
  listDocuments,
  saveField,
  approveField,
} from "../api/client";
import type {
  ApprovalStatus,
  Commitment,
  DocumentMeta,
  EngineResult,
  Examination,
  ExportSummary,
  Form,
  FieldName,
  FieldValue,
} from "../types/contracts";
import type { CommitmentRow } from "../components/RecordsTable";
import type { FieldSaveRequest, FieldApproveRequest } from "../components/SourceViewer";
import BatchSummary from "../components/BatchSummary";
import RecordsTable from "../components/RecordsTable";
import SourceViewer from "../components/SourceViewer";
import UploadModal from "../components/UploadModal";

// ----------------------------------------------------------------- state

interface State {
  documents: DocumentMeta[];
  loadingDocs: boolean;
  docsError: string | null;

  selectedDocId: string | null;
  engineResult: EngineResult | null;
  summary: ExportSummary | null;
  loadingDraft: boolean;
  draftError: string | null;

  downloadingDocId: string | null;

  /** True while a save or approve call is in flight */
  savingField: boolean;
  /** Per-field error from the last failed save/approve */
  fieldErrors: Partial<Record<FieldName, string>>;
  failedSaves: Partial<Record<FieldName, boolean>>;
}

type Action =
  | { type: "DOCS_LOADING" }
  | { type: "DOCS_LOADED"; payload: DocumentMeta[] }
  | { type: "DOCS_ERROR"; payload: string }
  | { type: "DOC_ADDED"; payload: DocumentMeta }
  | { type: "SELECT_DOC"; payload: string }
  | { type: "DRAFT_LOADING" }
  | { type: "DRAFT_LOADED"; payload: { engineResult: EngineResult; summary: ExportSummary; document: DocumentMeta } }
  | { type: "DRAFT_ERROR"; payload: string }
  | { type: "DOWNLOAD_START"; payload: string }
  | { type: "DOWNLOAD_END" }
  | { type: "FIELD_SAVING"; payload: FieldName }
  | {
      type: "FIELD_SAVED";
      payload: {
        examinationId: string;
        commitmentId: string | null;
        fieldName: FieldName;
        normalized: string;
        newVersion: number;
      };
    }
  | {
      type: "FIELD_APPROVED";
      payload: {
        examinationId: string;
        commitmentId: string | null;
        fieldName: FieldName;
        approvalStatus: ApprovalStatus;
        normalized: string;
        newVersion: number;
      };
    }
  | { type: "FIELD_ERROR"; payload: { fieldName: FieldName; error: string; operation: "save" | "approve" } }
  | { type: "FIELD_SAVE_DISCARDED"; payload: FieldName }
  | { type: "FIELD_ERRORS_CLEAR" };

// ---- immutable engineResult update helpers ----

function updateFieldInResult(
  er: EngineResult,
  examinationId: string,
  commitmentId: string | null,
  fieldName: FieldName,
  patch: Partial<{
    normalized: string;
    approval_status: ApprovalStatus;
    extraction_status: FieldValue["extraction_status"];
    partial: boolean;
    version: number;
  }>
): EngineResult {
  const next = structuredClone(er);
  for (const form of next.forms ?? [next.form]) {
    for (const exam of form.examinations) {
      if (exam.examination_id !== examinationId) continue;

      const patchFv = (fv: FieldValue): FieldValue =>
        fv.field_name === fieldName ? { ...fv, ...patch } : fv;

      if (commitmentId) {
        for (const cmt of exam.commitments) {
          if (cmt.commitment_id === commitmentId) {
            cmt.number = patchFv(cmt.number);
          }
        }
      } else {
        exam.first_name = patchFv(exam.first_name);
        exam.last_name = patchFv(exam.last_name);
        exam.israeli_id = patchFv(exam.israeli_id);
        exam.sample_received_date = patchFv(exam.sample_received_date);
        exam.sender = patchFv(exam.sender);
        exam.central_numbers = exam.central_numbers.map(patchFv);
      }
    }
  }
  return next;
}

function reducer(s: State, a: Action): State {
  switch (a.type) {
    case "DOCS_LOADING":   return { ...s, loadingDocs: true, docsError: null };
    case "DOCS_LOADED":    return { ...s, loadingDocs: false, documents: a.payload };
    case "DOCS_ERROR":     return { ...s, loadingDocs: false, docsError: a.payload };
    case "DOC_ADDED":      return {
      ...s,
      documents: [...s.documents, a.payload],
      selectedDocId: a.payload.file_id,
      engineResult: null,
      summary: null,
      draftError: null,
    };
    case "SELECT_DOC":     return {
      ...s,
      selectedDocId: a.payload,
      engineResult: null,
      summary: null,
      draftError: null,
    };
    case "DRAFT_LOADING":  return { ...s, loadingDraft: true, draftError: null };
    case "DRAFT_LOADED":   return {
      ...s,
      loadingDraft: false,
      engineResult: a.payload.engineResult,
      summary: a.payload.summary,
      documents: s.documents.map((doc) => doc.file_id === a.payload.document.file_id ? a.payload.document : doc),
    };
    case "DRAFT_ERROR":    return { ...s, loadingDraft: false, draftError: a.payload };
    case "DOWNLOAD_START": return { ...s, downloadingDocId: a.payload };
    case "DOWNLOAD_END":   return { ...s, downloadingDocId: null };

    case "FIELD_SAVING": {
      const fieldErrors = { ...s.fieldErrors };
      delete fieldErrors[a.payload];
      return { ...s, savingField: true, fieldErrors };
    }
    case "FIELD_SAVED": {
      if (!s.engineResult) return s;
      const { examinationId, commitmentId, fieldName, normalized, newVersion } = a.payload;
      const next = updateFieldInResult(
        s.engineResult, examinationId, commitmentId, fieldName,
        {
          normalized,
          version: newVersion,
          extraction_status: "COMPLETE",
          partial: false,
        }
        // approval_status intentionally NOT changed here — stays PENDING
      );
      const failedSaves = { ...s.failedSaves };
      delete failedSaves[fieldName];
      return { ...s, savingField: false, engineResult: next, fieldErrors: {}, failedSaves };
    }
    case "FIELD_APPROVED": {
      if (!s.engineResult) return s;
      const { examinationId, commitmentId, fieldName, approvalStatus, normalized, newVersion } =
        a.payload;
      const next = updateFieldInResult(
        s.engineResult, examinationId, commitmentId, fieldName,
        { approval_status: approvalStatus, normalized, version: newVersion }
      );
      return { ...s, savingField: false, engineResult: next, fieldErrors: {} };
    }
    case "FIELD_ERROR":    return {
      ...s,
      savingField: false,
      fieldErrors: { ...s.fieldErrors, [a.payload.fieldName]: a.payload.error },
      failedSaves: a.payload.operation === "save"
        ? { ...s.failedSaves, [a.payload.fieldName]: true }
        : s.failedSaves,
    };
    case "FIELD_SAVE_DISCARDED": {
      const fieldErrors = { ...s.fieldErrors };
      const failedSaves = { ...s.failedSaves };
      delete fieldErrors[a.payload];
      delete failedSaves[a.payload];
      return { ...s, fieldErrors, failedSaves };
    }
    case "FIELD_ERRORS_CLEAR": return { ...s, fieldErrors: {} };

    default: return s;
  }
}

const INIT: State = {
  documents: [], loadingDocs: false, docsError: null,
  selectedDocId: null, engineResult: null, summary: null,
  loadingDraft: false, draftError: null,
  downloadingDocId: null,
  savingField: false, fieldErrors: {}, failedSaves: {},
};

// ----------------------------------------------------------------- helpers

function apiError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "החיבור פג. התחברי מחדש; העריכה נשארה פתוחה.";
    const body = err.body as { detail?: { error?: string; reason?: string } | string } | null;
    const detail = body?.detail;
    const code = typeof detail === "object" ? detail?.error ?? detail?.reason : detail;
    if (err.status === 409) return "הנתונים השתנו בשרת. העריכה המקומית נשארה פתוחה אך לא נשמרה; הערך והגרסה מהשרת עודכנו. השווי את הערכים ונסי שוב.";
    if (err.status === 422) return code ? `השרת דחה את הערך (${code}). ניתן לתקן ולנסות שוב.` : "השרת דחה את הערך. ניתן לתקן ולנסות שוב.";
    return `שגיאת שרת (HTTP ${err.status})`;
  }
  if (err instanceof TypeError) return "שגיאת רשת. העריכה המקומית נשארה פתוחה; בדקי את החיבור ונסי לשמור שוב.";
  if (err instanceof Error) return err.message;
  return "שגיאה לא ידועה";
}

type TabId = "needs_review" | "ready" | "all";
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";

function FormWithoutCommitments({ form, document }: { form: Form; document: DocumentMeta }) {
  const pageRegion = form.page_regions.find((region) => typeof region.page === "number");
  const page = typeof pageRegion?.page === "number" ? pageRegion.page + 1 : 1;
  const sourceUrl = `${API_BASE_URL}/documents/${document.file_id}/file`;
  const extension = document.filename.split(".").pop()?.toLowerCase();

  return (
    <section className="alert alert--warn" aria-labelledby={`empty-form-${form.form_id}`}>
      <h3 id={`empty-form-${form.form_id}`} style={{ margin: "0 0 0.5rem" }}>
        לא זוהו התחייבויות — נדרשת בדיקה ידנית
      </h3>
      <p style={{ margin: "0 0 0.5rem" }}>טופס {form.form_id}</p>
      {form.issues.length > 0 && (
        <ul aria-label={`בעיות בטופס ${form.form_id}`}>
          {form.issues.map((issue) => (
            <li key={issue.issue_id}>
              <strong>{issue.reason}</strong>{issue.note ? `: ${issue.note}` : ""}
            </li>
          ))}
        </ul>
      )}
      <div className="doc-viewer" role="region" aria-label={`מסמך מקור עבור טופס ${form.form_id}`}>
        <p className="source-viewer__section-title">מסמך מקור · עמוד {page}</p>
        {extension === "pdf" ? (
          <object
            data={`${sourceUrl}#page=${page}`}
            type="application/pdf"
            className="doc-viewer__frame"
            aria-label={`מסמך ${document.filename}, עמוד ${page}`}
          >
            <a href={sourceUrl} className="btn btn--secondary btn--sm">פתחי את מסמך המקור</a>
          </object>
        ) : extension === "jpg" || extension === "jpeg" || extension === "png" ? (
          <img src={sourceUrl} alt={`מסמך מקור ${document.filename}`} className="doc-viewer__image" />
        ) : (
          <a href={sourceUrl} className="btn btn--secondary btn--sm">פתחי את מסמך המקור</a>
        )}
      </div>
    </section>
  );
}

const TABS: { id: TabId; label: string }[] = [
  { id: "needs_review", label: "דורשות בדיקה" },
  { id: "ready",        label: "מוכנות" },
  { id: "all",          label: "הכול" },
];

function isReady(exam: Examination, c: Commitment): boolean {
  const fields = [
    exam.first_name, exam.last_name, exam.israeli_id,
    exam.sample_received_date, exam.sender, c.number,
    ...exam.central_numbers,
  ];
  return fields.length > 0 && fields.every(
    (f) =>
      f.approval_status === "HUMAN_APPROVED" ||
      f.approval_status === "AUTO_APPROVED" ||
      f.approval_status === "CONFIRMED_ABSENT"
  );
}

function firstFieldForReview(row: CommitmentRow): FieldName {
  const fields = [
    row.exam.first_name,
    row.exam.last_name,
    row.exam.israeli_id,
    row.exam.sample_received_date,
    row.exam.sender,
    row.commitment.number,
  ];
  return fields.find((field) => field.approval_status === "PENDING")?.field_name
    ?? fields[0]!.field_name;
}

function formIdForExamination(result: EngineResult, examinationId: string): string {
  return (result.forms ?? [result.form]).find((form) =>
    form.examinations.some((exam) => exam.examination_id === examinationId)
  )?.form_id ?? result.form.form_id;
}

// ----------------------------------------------------------------- component

export default function ReviewWorkspace() {
  const [state, dispatch] = useReducer(reducer, INIT);
  const sessionGeneration = useRef(getAuthSessionGeneration());
  const [tab, setTab] = useState<TabId>("needs_review");
  const [search, setSearch] = useState("");
  const [selectedCommitmentId, setSelectedCommitmentId] = useState<string | null>(null);
  const [selectedFieldName, setSelectedFieldName] = useState<FieldName | null>(null);
  const [selectedFieldIndex, setSelectedFieldIndex] = useState<number | null>(null);
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [sourcePanelWidth, setSourcePanelWidth] = useState(360);
  const resizeStart = useRef<{ pointerId: number; x: number; width: number } | null>(null);

  useEffect(() => {
    const handlePointerMove = (event: PointerEvent) => {
      const start = resizeStart.current;
      if (!start || start.pointerId !== event.pointerId) return;
      const parentWidth = document.querySelector(".records-panel")?.clientWidth ?? window.innerWidth;
      const maximum = Math.max(280, Math.min(640, parentWidth * 0.6));
      setSourcePanelWidth(Math.max(260, Math.min(maximum, start.width + start.x - event.clientX)));
    };
    const stopResize = (event: PointerEvent) => {
      if (resizeStart.current?.pointerId === event.pointerId) resizeStart.current = null;
    };
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", stopResize);
    window.addEventListener("pointercancel", stopResize);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopResize);
      window.removeEventListener("pointercancel", stopResize);
    };
  }, []);

  // ---- Load documents on mount ----
  useEffect(() => {
    let cancelled = false;
    dispatch({ type: "DOCS_LOADING" });
    listDocuments()
      .then((docs) => {
        if (!cancelled && isAuthSessionCurrent(sessionGeneration.current)) {
          dispatch({ type: "DOCS_LOADED", payload: docs });
        }
      })
      .catch((err: unknown) => {
        if (!cancelled && isAuthSessionCurrent(sessionGeneration.current)) {
          dispatch({ type: "DOCS_ERROR", payload: apiError(err) });
        }
      });
    return () => { cancelled = true; };
  }, []);

  // ---- Auto-select first doc ----
  useEffect(() => {
    if (!state.selectedDocId && state.documents.length > 0 && state.documents[0]) {
      dispatch({ type: "SELECT_DOC", payload: state.documents[0].file_id });
    }
  }, [state.documents, state.selectedDocId]);

  // ---- Load draft + summary when doc selected ----
  useEffect(() => {
    if (!state.selectedDocId) return;
    let cancelled = false;
    const id = state.selectedDocId;
    dispatch({ type: "DRAFT_LOADING" });
    initDrafts(id)
      .then(() => Promise.all([getDrafts(id), getExportSummary(id), getDocument(id)]))
      .then(([engineResult, summary, document]) => {
        if (!cancelled && isAuthSessionCurrent(sessionGeneration.current)) {
          dispatch({ type: "DRAFT_LOADED", payload: { engineResult, summary, document } });
        }
      })
      .catch((err: unknown) => {
        if (!cancelled && isAuthSessionCurrent(sessionGeneration.current)) {
          dispatch({ type: "DRAFT_ERROR", payload: apiError(err) });
        }
      });
    return () => { cancelled = true; };
  }, [state.selectedDocId]);

  // ---- Reset selection when doc changes ----
  useEffect(() => {
    setSelectedCommitmentId(null);
    setSelectedFieldName(null);
    setSelectedFieldIndex(null);
  }, [state.selectedDocId]);

  const handleDocSelect = useCallback((id: string) => {
    dispatch({ type: "SELECT_DOC", payload: id });
  }, []);

  // ---- Upload ----
  const handleUploaded = useCallback((doc: DocumentMeta) => {
    if (!isAuthSessionCurrent(sessionGeneration.current)) return;
    dispatch({ type: "DOC_ADDED", payload: doc });
    setShowUploadModal(false);
  }, []);

  const refreshReviewData = useCallback(async (fileId: string) => {
    const [engineResult, summary, document] = await Promise.all([
      getDrafts(fileId), getExportSummary(fileId), getDocument(fileId),
    ]);
    if (isAuthSessionCurrent(sessionGeneration.current) && state.selectedDocId === fileId) {
      dispatch({ type: "DRAFT_LOADED", payload: { engineResult, summary, document } });
    }
  }, [state.selectedDocId]);

  // ---- Excel download ----
  const handleDownload = useCallback(async () => {
    if (!isAuthSessionCurrent(sessionGeneration.current)) return;
    const doc = state.documents.find((d) => d.file_id === state.selectedDocId);
    if (!doc) return;
    dispatch({ type: "DOWNLOAD_START", payload: doc.file_id });
    try {
      let blob: Blob;
      try {
        blob = await downloadExcel(doc.file_id);
      } catch (err: unknown) {
        if (!(err instanceof ApiError) || err.status !== 409) throw err;
        const body = err.body as { detail?: { needs_confirmation?: boolean; summary?: ExportSummary } } | null;
        const partial = body?.detail;
        if (!partial?.needs_confirmation) throw err;
        const warnings = partial.summary?.warnings ?? [];
        const message = [
          "הייצוא חלקי. האם להמשיך?",
          partial.summary ? `שורות: ${partial.summary.rows}; שורות לבדיקה: ${partial.summary.pending_rows}.` : "",
          ...warnings,
        ].filter(Boolean).join("\n");
        if (!window.confirm(message)) return;
        blob = await downloadExcel(doc.file_id, true);
      }
      if (!isAuthSessionCurrent(sessionGeneration.current)) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = state.summary?.filename || doc.filename.replace(/\.(pdf|jpe?g|png)$/i, "") + "_export.xlsx";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      if (isAuthSessionCurrent(sessionGeneration.current)) alert(apiError(err));
    } finally {
      if (isAuthSessionCurrent(sessionGeneration.current)) dispatch({ type: "DOWNLOAD_END" });
    }
  }, [state.documents, state.selectedDocId, state.summary]);

  // ---- SAVE a field value (no approval) ----
  const handleFieldSave = useCallback(
    async (req: FieldSaveRequest) => {
      if (!isAuthSessionCurrent(sessionGeneration.current) || !state.engineResult || !state.selectedDocId) return;
      dispatch({ type: "FIELD_SAVING", payload: req.field_name });
      try {
        const result = await saveField(
          state.selectedDocId,
          formIdForExamination(state.engineResult, req.examination_id),
          req.examination_id,
          req.commitment_id,
          req.field_name,
          req.normalized,
          req.version,
          req.field_name === "central_numbers" && req.index !== undefined
            ? { index: req.index, expected_value: req.expected_value }
            : undefined
        );
        if (!isAuthSessionCurrent(sessionGeneration.current)) return;
        // Server confirmed → apply. approval_status stays PENDING (done in reducer).
        dispatch({
          type: "FIELD_SAVED",
          payload: {
            examinationId: req.examination_id,
            commitmentId: req.commitment_id,
            fieldName: req.field_name,
            normalized: result.normalized,
            newVersion: result.version,
          },
        });
      } catch (err: unknown) {
        if (!isAuthSessionCurrent(sessionGeneration.current)) return;
        dispatch({
          type: "FIELD_ERROR",
          payload: { fieldName: req.field_name, error: apiError(err), operation: "save" },
        });
        if (err instanceof ApiError && err.status === 409 && state.selectedDocId) {
          try { await refreshReviewData(state.selectedDocId); } catch { /* Keep the local edit and conflict message. */ }
        }
      }
    },
    [state.engineResult, state.selectedDocId, refreshReviewData]
  );

  const handleFieldSaveCancel = useCallback((fieldName: FieldName) => {
    dispatch({ type: "FIELD_SAVE_DISCARDED", payload: fieldName });
  }, []);

  // ---- APPROVE a saved field ----
  const handleFieldApprove = useCallback(
    async (req: FieldApproveRequest) => {
      if (!isAuthSessionCurrent(sessionGeneration.current) || !state.engineResult || !state.selectedDocId) return false;
      dispatch({ type: "FIELD_SAVING", payload: req.field_name });
      try {
        const result = await approveField(
          state.selectedDocId,
          formIdForExamination(state.engineResult, req.examination_id),
          req.examination_id,
          req.commitment_id,
          req.field_name,
          req.approval_status,
          req.normalized,
          req.version,
          req.field_name === "central_numbers" && req.index !== undefined
            ? { index: req.index, expected_value: req.expected_value }
            : undefined
        );
        if (!isAuthSessionCurrent(sessionGeneration.current)) return false;
        dispatch({
          type: "FIELD_APPROVED",
          payload: {
            examinationId: req.examination_id,
            commitmentId: req.commitment_id,
            fieldName: req.field_name,
            approvalStatus: result.approval_status,
            normalized: req.normalized,
            newVersion: result.version,
          },
        });
        return true;
      } catch (err: unknown) {
        if (!isAuthSessionCurrent(sessionGeneration.current)) return false;
        dispatch({
          type: "FIELD_ERROR",
          payload: { fieldName: req.field_name, error: apiError(err), operation: "approve" },
        });
        if (err instanceof ApiError && err.status === 409 && state.selectedDocId) {
          try { await refreshReviewData(state.selectedDocId); } catch { /* Keep the local error visible. */ }
        }
        return false;
      }
    },
    [state.engineResult, state.selectedDocId, refreshReviewData]
  );

  // ---- Build flat rows ----
  const allRows = useMemo<CommitmentRow[]>(() => {
    if (!state.engineResult) return [];
    return (state.engineResult.forms ?? [state.engineResult.form]).flatMap((form) =>
      form.examinations.flatMap((exam) => exam.commitments.map((c) => ({
        commitmentId: c.commitment_id,
        exam,
        commitment: c,
      })))
    );
  }, [state.engineResult]);

  const formsWithoutCommitments = useMemo(
    () => (state.engineResult?.forms ?? (state.engineResult ? [state.engineResult.form] : []))
      .filter((form) => !form.examinations.some((exam) => exam.commitments.length > 0)),
    [state.engineResult]
  );

  const needsReviewCount = useMemo(
    () => allRows.filter(({ exam, commitment }) => !isReady(exam, commitment)).length,
    [allRows]
  );
  const readyCount = useMemo(
    () => allRows.filter(({ exam, commitment }) => isReady(exam, commitment)).length,
    [allRows]
  );

  const visibleRows = useMemo(() => {
    let rows = allRows;
    if (tab === "needs_review") rows = rows.filter(({ exam, commitment }) => !isReady(exam, commitment));
    if (tab === "ready")        rows = rows.filter(({ exam, commitment }) => isReady(exam, commitment));
    const q = search.trim().toLowerCase();
    if (q) {
      rows = rows.filter(({ exam, commitment }) => {
        const text = [
          exam.first_name.normalized, exam.first_name.raw,
          exam.last_name.normalized, exam.last_name.raw,
          exam.israeli_id.normalized, exam.israeli_id.raw,
          commitment.number.normalized, commitment.number.raw,
          ...exam.central_numbers.map((f) => f.normalized ?? f.raw),
          exam.sender.normalized, exam.sender.raw,
        ].filter(Boolean).join(" ").toLowerCase();
        return text.includes(q);
      });
    }
    return rows;
  }, [allRows, tab, search]);

  const selectedRow = useMemo(
    () => allRows.find((r) => r.commitmentId === selectedCommitmentId) ?? null,
    [allRows, selectedCommitmentId]
  );

  const selectedForm = selectedRow && state.engineResult
    ? (state.engineResult.forms ?? [state.engineResult.form]).find((form) => form.form_id === selectedRow.exam.form_id)
    : undefined;
  const approvalBlocked = !!selectedForm?.issues.some((issue) =>
    ["REVIEW_RELINK_REQUIRED", "UNREADABLE_DOCUMENT", "SPLIT_SUSPECT", "TECHNICAL_FAILURE"].includes(issue.reason) &&
    issue.resolution_state !== "CLOSED" &&
    (issue.commitment_id === null || issue.commitment_id === selectedRow?.commitment.commitment_id)
  );

  const currentDoc = state.documents.find((d) => d.file_id === state.selectedDocId);
  const extractionFailed = currentDoc?.processing_status === "FAILED";
  const extractionPending = currentDoc?.processing_status === "QUEUED" || currentDoc?.processing_status === "RUNNING";
  const isDownloading = !!state.downloadingDocId;

  // ----------------------------------------------------------------- render

  return (
    <div className="review-workspace">
      <div className="review-heading">
        <div>
          <p className="review-heading__eyebrow">מרכז סקירה חכם</p>
          <h1 className="review-heading__title">סקירת התחייבויות</h1>
        </div>
        <span className="review-heading__note">כל המסמכים והנתונים במקום אחד</span>
      </div>

      {/* ---- Document selector ---- */}
      <div className="review-controls">
        <label
          htmlFor="doc-select"
          className="review-controls__label"
        >
          מסמך:
        </label>
        <select
          id="doc-select"
          className="filter-select"
          value={state.selectedDocId ?? ""}
          onChange={(e) => handleDocSelect(e.target.value)}
          disabled={state.loadingDocs || state.documents.length === 0}
        >
          {state.loadingDocs && <option value="">טוען מסמכים…</option>}
          {state.documents.map((d) => (
            <option key={d.file_id} value={d.file_id}>{d.filename}</option>
          ))}
        </select>

        {currentDoc && (
          <span
            className={`badge badge--proc-${currentDoc.processing_status.toLowerCase()}`}
            title="סטטוס עיבוד מסמך"
          >
            {{ QUEUED: "ממתין", RUNNING: "בעיבוד", DONE: "עובד", FAILED: "נכשל" }[currentDoc.processing_status]}
          </span>
        )}

        <button
          type="button"
          className="btn btn--secondary btn--sm"
          onClick={() => setShowUploadModal(true)}
          aria-label="העלה מסמך חדש"
        >
          ⬆ העלאה
        </button>
      </div>

      {/* Errors */}
      {state.docsError && <div className="alert alert--error" role="alert">{state.docsError}</div>}
      {state.draftError && <div className="alert alert--error" role="alert">{state.draftError}</div>}
      {extractionFailed && <div className="alert alert--error" role="alert">החילוץ נכשל. הנתונים עשויים להיות חסרים; בדקי והזיני ערכים ידנית מול המסמך המקורי.</div>}
      {extractionPending && <div className="alert alert--warn" role="status">המסמך עדיין בעיבוד. ייתכן שהתוצאות טרם הושלמו.</div>}

      {/* Batch summary */}
      {currentDoc && state.summary && (
        <BatchSummary doc={currentDoc} summary={state.summary} />
      )}

      {/* ---- Tabs ---- */}
      <div className="review-tabs" role="tablist" aria-label="תצוגת התחייבויות">
        {TABS.map(({ id, label }) => {
          const count = id === "needs_review" ? needsReviewCount : id === "ready" ? readyCount : allRows.length;
          return (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              className={`review-tab${tab === id ? " review-tab--active" : ""}`}
              onClick={() => {
                setTab(id);
                setSelectedCommitmentId(null);
                setSelectedFieldName(null);
                setSelectedFieldIndex(null);
              }}
            >
              {label}
              <span
                className={`review-tab__count${id === "needs_review" && needsReviewCount > 0 ? " review-tab__count--warn" : ""}`}
                aria-label={`${count} פריטים`}
              >
                {count}
              </span>
            </button>
          );
        })}
      </div>

      {/* ---- Toolbar ---- */}
      <div className="records-toolbar">
        <div className="search-box">
          <span className="search-box__icon" aria-hidden="true">&#x2315;</span>
          <input
            type="search"
            className="search-box__input"
            placeholder="חיפוש לפי שם, ת.ז., מספר מחויבות…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="חיפוש בהתחייבויות"
          />
        </div>
        <select
          className="filter-select"
          defaultValue=""
          aria-label="סינון לפי סטטוס"
          disabled
          title="יהיה זמין בשלב הבא"
        >
          <option value="">כל הסטטוסים</option>
        </select>
        <div className="toolbar-spacer" />
        <div className="toolbar-actions">
          <button
            type="button"
            className="btn btn--secondary"
            onClick={handleDownload}
            disabled={!state.selectedDocId || isDownloading || !state.engineResult}
            aria-label="ייצא לאקסל"
          >
            {isDownloading ? "מייצא…" : "ייצא Excel"}
          </button>
          <button
            type="button"
            className="btn btn--teal"
            disabled
            title="עדכן נתונים — יהיה זמין לאחר אישור שדות"
          >
            עדכן נתונים
          </button>
        </div>
      </div>

      {/* ---- Main panel ---- */}
      {state.loadingDraft ? (
        <div className="loading-row">
          <span className="spinner" aria-hidden="true" />
          <span>טוען התחייבויות…</span>
        </div>
      ) : state.engineResult ? (
        <div className="records-panel">
          {allRows.length > 0 ? (
            <RecordsTable
              rows={visibleRows}
              selectedId={selectedCommitmentId}
              allowEditing={tab === "needs_review"}
              saving={state.savingField}
              fieldErrors={state.fieldErrors}
              onFieldSave={handleFieldSave}
              onFieldSaveCancel={handleFieldSaveCancel}
              onSelect={(id) => {
                const row = allRows.find((item) => item.commitmentId === id);
                setSelectedCommitmentId(id);
                setSelectedFieldName(row ? firstFieldForReview(row) : null);
                setSelectedFieldIndex(null);
              }}
              onFieldSelect={(id, fieldName, index) => {
                setSelectedCommitmentId(id);
                setSelectedFieldName(fieldName);
                setSelectedFieldIndex(index ?? null);
              }}
            />
          ) : (
            <div className="records-table-wrap" role="region" aria-label="טפסים לבדיקה ידנית">
              {formsWithoutCommitments.map((form) => currentDoc && (
                <FormWithoutCommitments key={form.form_id} form={form} document={currentDoc} />
              ))}
            </div>
          )}
          {allRows.length > 0 && formsWithoutCommitments.length > 0 && currentDoc && (
            <div className="records-table-wrap" role="region" aria-label="טפסים ללא התחייבויות">
              {formsWithoutCommitments.map((form) => (
                <FormWithoutCommitments key={form.form_id} form={form} document={currentDoc} />
              ))}
            </div>
          )}
          {selectedRow && currentDoc && (
            <>
              <div
                className="review-resize-handle"
                role="separator"
                aria-orientation="vertical"
                aria-label="שינוי רוחב חלונית המקור"
                aria-valuemin={260}
                aria-valuemax={640}
                aria-valuenow={Math.round(sourcePanelWidth)}
                tabIndex={0}
                onPointerDown={(event) => {
                  event.preventDefault();
                  resizeStart.current = {
                    pointerId: event.pointerId,
                    x: event.clientX,
                    width: sourcePanelWidth,
                  };
                }}
                onKeyDown={(event) => {
                  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                    event.preventDefault();
                    setSourcePanelWidth((width) =>
                      Math.max(260, Math.min(640, width + (event.key === "ArrowLeft" ? 16 : -16)))
                    );
                  }
                }}
              />
              <SourceViewer
                key={`${currentDoc.file_id}:${selectedRow.commitmentId}`}
                fileId={currentDoc.file_id}
                filename={currentDoc.filename}
                pageCount={currentDoc.page_count}
                panelWidth={sourcePanelWidth}
                selectedFieldName={selectedFieldName}
                selectedFieldIndex={selectedFieldIndex}
                onSelectField={(fieldName, index) => {
                  setSelectedFieldName(fieldName);
                  setSelectedFieldIndex(index ?? null);
                }}
                exam={selectedRow.exam}
                commitment={selectedRow.commitment}
                saving={state.savingField}
                approvalBlocked={approvalBlocked}
                fieldErrors={state.fieldErrors}
                failedSaves={state.failedSaves}
                onClose={() => {
                  setSelectedCommitmentId(null);
                  setSelectedFieldName(null);
                  setSelectedFieldIndex(null);
                }}
                onFieldApprove={handleFieldApprove}
              />
            </>
          )}
        </div>
      ) : !state.docsError && !state.draftError ? (
        <div className="empty-state">
          <p className="empty-state__title">בחרי מסמך לסקירה</p>
          <p className="empty-state__body">בחרי מסמך מהרשימה למעלה להצגת ההתחייבויות.</p>
        </div>
      ) : null}

      {/* ---- Upload Modal ---- */}
      {showUploadModal && (
        <UploadModal
          onClose={() => setShowUploadModal(false)}
          onUploaded={handleUploaded}
        />
      )}
    </div>
  );
}
