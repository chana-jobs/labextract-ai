/**
 * LabExtract AI — WorkspacePage
 *
 * Lists all documents in the workspace and allows the user to:
 *  - See processing status for each file.
 *  - Select a file to view its extracted forms (drafts).
 *  - Download the Excel export for a ready file.
 *
 * Uses native fetch via src/api/client.ts — no Axios.
 * All data displayed in mock mode is SYNTHETIC (see src/mock/syntheticForms.ts).
 */

import { useEffect, useReducer, useCallback } from "react";
import {
  ApiError,
  downloadExcel,
  getExportSummary,
  getDrafts,
  listDocuments,
} from "../api/client";
import type { DocumentMeta, EngineResult, ExportSummary } from "../types/contracts";
import FormGrid from "../components/FormGrid";

// ----------------------------------------------------------------- state

interface State {
  documents: DocumentMeta[];
  loadingDocs: boolean;
  docsError: string | null;

  selectedFileId: string | null;
  drafts: EngineResult | null;
  summary: ExportSummary | null;
  loadingDrafts: boolean;
  draftsError: string | null;

  downloadingFileId: string | null;
}

type Action =
  | { type: "DOCS_LOADING" }
  | { type: "DOCS_LOADED"; payload: DocumentMeta[] }
  | { type: "DOCS_ERROR"; payload: string }
  | { type: "SELECT_FILE"; payload: string }
  | { type: "DRAFTS_LOADING" }
  | { type: "DRAFTS_LOADED"; payload: { drafts: EngineResult; summary: ExportSummary } }
  | { type: "DRAFTS_ERROR"; payload: string }
  | { type: "DOWNLOAD_START"; payload: string }
  | { type: "DOWNLOAD_END" };

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "DOCS_LOADING":
      return { ...state, loadingDocs: true, docsError: null };
    case "DOCS_LOADED":
      return { ...state, loadingDocs: false, documents: action.payload };
    case "DOCS_ERROR":
      return { ...state, loadingDocs: false, docsError: action.payload };
    case "SELECT_FILE":
      return {
        ...state,
        selectedFileId: action.payload,
        drafts: null,
        summary: null,
        draftsError: null,
      };
    case "DRAFTS_LOADING":
      return { ...state, loadingDrafts: true, draftsError: null };
    case "DRAFTS_LOADED":
      return {
        ...state,
        loadingDrafts: false,
        drafts: action.payload.drafts,
        summary: action.payload.summary,
      };
    case "DRAFTS_ERROR":
      return { ...state, loadingDrafts: false, draftsError: action.payload };
    case "DOWNLOAD_START":
      return { ...state, downloadingFileId: action.payload };
    case "DOWNLOAD_END":
      return { ...state, downloadingFileId: null };
    default:
      return state;
  }
}

const INITIAL_STATE: State = {
  documents: [],
  loadingDocs: false,
  docsError: null,
  selectedFileId: null,
  drafts: null,
  summary: null,
  loadingDrafts: false,
  draftsError: null,
  downloadingFileId: null,
};

// ----------------------------------------------------------------- helpers

function errorMessage(err: unknown): string {
  // I-8: never leak patient data from errors into UI/logs
  if (err instanceof ApiError) {
    return `שגיאת שרת: HTTP ${err.status}`;
  }
  if (err instanceof Error) return err.message;
  return "שגיאה לא ידועה";
}

function statusLabel(s: DocumentMeta["processing_status"]): string {
  const map: Record<DocumentMeta["processing_status"], string> = {
    QUEUED: "ממתין",
    RUNNING: "בעיבוד",
    DONE: "הושלם",
    FAILED: "נכשל",
  };
  return map[s];
}

function statusBadgeClass(s: DocumentMeta["processing_status"]): string {
  if (s === "DONE") return "badge badge--ready";
  if (s === "FAILED") return "badge badge--failed";
  return "badge badge--needs-review";
}

// ----------------------------------------------------------------- component

export default function WorkspacePage() {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);

  // Load document list on mount
  useEffect(() => {
    let cancelled = false;
    dispatch({ type: "DOCS_LOADING" });
    listDocuments()
      .then((docs) => {
        if (!cancelled) dispatch({ type: "DOCS_LOADED", payload: docs });
      })
      .catch((err: unknown) => {
        if (!cancelled) dispatch({ type: "DOCS_ERROR", payload: errorMessage(err) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Load drafts + summary when a file is selected
  useEffect(() => {
    if (!state.selectedFileId) return;
    let cancelled = false;
    const fileId = state.selectedFileId;
    dispatch({ type: "DRAFTS_LOADING" });

    Promise.all([getDrafts(fileId), getExportSummary(fileId)])
      .then(([drafts, summary]) => {
        if (!cancelled)
          dispatch({ type: "DRAFTS_LOADED", payload: { drafts, summary } });
      })
      .catch((err: unknown) => {
        if (!cancelled)
          dispatch({ type: "DRAFTS_ERROR", payload: errorMessage(err) });
      });

    return () => {
      cancelled = true;
    };
  }, [state.selectedFileId]);

  const handleSelect = useCallback((fileId: string) => {
    dispatch({ type: "SELECT_FILE", payload: fileId });
  }, []);

  const handleDownload = useCallback(
    async (fileId: string, filename: string) => {
      dispatch({ type: "DOWNLOAD_START", payload: fileId });
      try {
        const blob = await downloadExcel(fileId);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename.replace(/\.pdf$/i, "") + "_export.xlsx";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      } catch (err: unknown) {
        // Surface error without leaking patient data (I-8)
        alert(errorMessage(err));
      } finally {
        dispatch({ type: "DOWNLOAD_END" });
      }
    },
    []
  );

  // ---- render

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>מסמכים בסביבת העבודה</h2>

      {/* Document list */}
      {state.loadingDocs && <p>טוען מסמכים…</p>}
      {state.docsError && (
        <p role="alert" style={{ color: "red" }}>
          {state.docsError}
        </p>
      )}

      {!state.loadingDocs && state.documents.length === 0 && !state.docsError && (
        <p>אין מסמכים בסביבת העבודה.</p>
      )}

      {state.documents.length > 0 && (
        <table aria-label="רשימת מסמכים">
          <thead>
            <tr>
              <th scope="col">שם קובץ</th>
              <th scope="col">סטטוס</th>
              <th scope="col">תאריך העלאה</th>
              <th scope="col">עמודים</th>
              <th scope="col">פעולות</th>
            </tr>
          </thead>
          <tbody>
            {state.documents.map((doc) => (
              <tr
                key={doc.file_id}
                style={{
                  background:
                    state.selectedFileId === doc.file_id ? "#eef2ff" : undefined,
                }}
              >
                <td>{doc.filename}</td>
                <td>
                  <span className={statusBadgeClass(doc.processing_status)}>
                    {statusLabel(doc.processing_status)}
                  </span>
                </td>
                <td>
                  {new Date(doc.uploaded_at ?? doc.upload_date ?? "").toLocaleString("he-IL", {
                    dateStyle: "short",
                    timeStyle: "short",
                  })}
                </td>
                <td>{doc.page_count ?? "—"}</td>
                <td style={{ display: "flex", gap: "0.5rem" }}>
                  <button
                    onClick={() => handleSelect(doc.file_id)}
                    disabled={doc.processing_status !== "DONE"}
                    className="secondary"
                    aria-label={`צפה בטופס ${doc.filename}`}
                  >
                    צפה בטופס
                  </button>
                  <button
                    onClick={() => handleDownload(doc.file_id, doc.filename)}
                    disabled={
                      doc.processing_status !== "DONE" ||
                      state.downloadingFileId === doc.file_id
                    }
                    aria-label={`הורד Excel עבור ${doc.filename}`}
                  >
                    {state.downloadingFileId === doc.file_id
                      ? "מוריד…"
                      : "Excel ↓"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* Export summary */}
      {state.summary && (
        <div
          style={{
            marginTop: "1.5rem",
            background: "#fff",
            border: "1px solid #d0d5dd",
            borderRadius: "8px",
            padding: "1rem",
          }}
          aria-label="סיכום ייצוא"
        >
          <h3 style={{ marginTop: 0 }}>סיכום ייצוא</h3>
          <ul>
            <li>סה״כ מחויבויות: {state.summary.rows}</li>
            <li>מוכן לייצוא: {state.summary.ready}</li>
            <li>דורש בדיקה: {state.summary.pending_rows}</li>
            {state.summary.is_partial && (
              <li>
                <strong>שים לב:</strong> ישנם ערכים חלקיים בטופס.
              </li>
            )}
          </ul>
          {state.summary.warnings.length > 0 && (
            <>
              <h4>אזהרות:</h4>
              <ul>
                {state.summary.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {/* Form grid */}
      {state.loadingDrafts && <p style={{ marginTop: "1.5rem" }}>טוען טופס…</p>}
      {state.draftsError && (
        <p role="alert" style={{ color: "red", marginTop: "1.5rem" }}>
          {state.draftsError}
        </p>
      )}
      {state.drafts && (
        <FormGrid engineResult={state.drafts} />
      )}
    </div>
  );
}
