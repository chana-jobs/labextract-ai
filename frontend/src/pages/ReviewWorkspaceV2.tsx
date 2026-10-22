/**
 * ReviewWorkspaceV2 — commitment-centred review workbench.
 *
 * - The commitment grid is the centre; no document must be chosen first.
 * - One selection rule: tab + filters → one set (model.select) used by the grid,
 *   Excel and the source-form actions. Actions the API cannot serve for that
 *   exact set are disabled with the reason (Backend dependency).
 * - Status comes from server-persisted approval statuses (model.rowStatus).
 * - Save and approve are separate calls to the existing API; after each, the
 *   document's drafts are re-read from the server (no optimistic state).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, approveField, confirmCommitmentCount, downloadExcel, getExportSummary, saveField } from "../api/client";
import type { ExportSummary, FieldName } from "../types/contracts";
import SourceViewer, { type FieldApproveRequest } from "../components/SourceViewer";
import UploadModal from "../components/UploadModal";
import CentralApproval from "../v2/components/CentralApproval";
import CommitmentGrid, { type SaveCellRequest } from "../v2/components/CommitmentGrid";
import FilterBar from "../v2/components/FilterBar";
import FormSource from "../v2/components/FormSource";
import FormsList from "../v2/components/FormsList";
import ViewTabs from "../v2/components/ViewTabs";
import { errorMessage } from "../v2/errors";
import {
  buildRows,
  cellKey,
  EMPTY_FILTERS,
  excelAvailability,
  failedPages,
  fieldText,
  latestDoc,
  select,
  selectedSourceForms,
  sourceDownloadAvailability,
  type CellKey,
  type CommitmentRowV2,
  type Filters,
  type TabId,
} from "../v2/model";
import { useWorkspaceData } from "../v2/useWorkspaceData";
import "../v2/v2.css";

const PAGE_SIZE = 50;
const USE_MOCK = import.meta.env.VITE_USE_MOCK === "true";
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";

type Pane =
  | { kind: "row"; key: string; field: FieldName | null; index: number | null }
  | { kind: "forms"; index: number }
  | null;

type ExcelCheck =
  | { state: "idle" }
  | { state: "checking" }
  | { state: "ok"; summary: ExportSummary }
  | { state: "mismatch"; serverRows: number }
  | { state: "error"; message: string };

const DOC_STATE_LABEL = { loading: "טוען…", pending: "ממתין לעיבוד", processing: "בעיבוד…", error: "שגיאה" } as const;

/**
 * Field to focus when a whole row is selected, so the viewer opens at this commitment's
 * page (not page 1 of a multi-form PDF): its commitment number, else the first field with evidence.
 */
function defaultSourceField(row: CommitmentRowV2): FieldName | null {
  const { exam, commitment } = row;
  const candidates = [commitment.number, exam.first_name, exam.last_name, exam.israeli_id, exam.sample_received_date, exam.sender];
  return candidates.find((f) => f.evidence.length > 0)?.field_name ?? null;
}

export default function ReviewWorkspaceV2() {
  const data = useWorkspaceData();
  const { docs, live } = data;

  const [tab, setTab] = useState<TabId>("review");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [page, setPage] = useState(1);
  const [pane, setPane] = useState<Pane>(null);
  const [paneWidth, setPaneWidth] = useState(420);
  const [maximized, setMaximized] = useState(false);
  const [savingCell, setSavingCell] = useState<CellKey | null>(null);
  const [approving, setApproving] = useState(false);
  const [cellErrors, setCellErrors] = useState<Record<CellKey, string>>({});
  const [dirtyCells, setDirtyCells] = useState<ReadonlySet<CellKey>>(new Set());
  const [resizing, setResizing] = useState(false);
  const [notice, setNotice] = useState<{ tone: "info" | "error"; text: string } | null>(null);
  const [showUpload, setShowUpload] = useState(false);
  const [excelCheck, setExcelCheck] = useState<ExcelCheck>({ state: "idle" });
  const [downloading, setDownloading] = useState(false);
  const resize = useRef<{ pointerId: number; x: number; width: number } | null>(null);

  // ---------------------------------------------------------------- selection (one rule)

  const sel = useMemo(() => select(docs, tab, filters), [docs, tab, filters]);
  const allRows = useMemo(() => buildRows(docs), [docs]);
  const pageCount = Math.max(1, Math.ceil(sel.rows.length / PAGE_SIZE));
  const pageRows = sel.rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const senders = useMemo(
    () => [...new Set(buildRows(sel.scopeDocs).map((r) => fieldText(r.exam.sender)).filter(Boolean))].sort(),
    [sel.scopeDocs]
  );
  const latest = latestDoc(docs);
  const sourceForms = useMemo(() => selectedSourceForms(tab, sel), [tab, sel]);
  const excel = useMemo(() => excelAvailability(docs, tab, sel), [docs, tab, sel]);
  const sourceDownload = useMemo(() => sourceDownloadAvailability(docs, tab, sel), [docs, tab, sel]);
  const hasUnsavedEdit = Object.keys(cellErrors).length > 0 || dirtyCells.size > 0;

  // Idempotent: returns the same Set when nothing changes, so editors may report on every render.
  const handleDirty = useCallback((key: CellKey, dirty: boolean) => {
    setDirtyCells((prev) => {
      if (prev.has(key) === dirty) return prev;
      const next = new Set(prev);
      if (dirty) next.add(key); else next.delete(key);
      return next;
    });
  }, []);

  useEffect(() => { setPage(1); }, [tab, filters]);
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [page, pageCount]);

  const selectedRow = pane?.kind === "row" ? allRows.find((r) => r.key === pane.key) ?? null : null;

  // Server confirmation of the export set: the count must match the screen.
  const excelDocLoad = excel.ok ? docs.find((d) => d.meta.file_id === excel.fileId)?.load : undefined;
  useEffect(() => {
    if (!excel.ok) { setExcelCheck({ state: "idle" }); return; }
    let cancelled = false;
    setExcelCheck({ state: "checking" });
    getExportSummary(excel.fileId)
      .then((summary) => {
        if (cancelled || !live()) return;
        setExcelCheck(summary.rows === excel.rows ? { state: "ok", summary } : { state: "mismatch", serverRows: summary.rows });
      })
      .catch((err: unknown) => { if (!cancelled && live()) setExcelCheck({ state: "error", message: errorMessage(err) }); });
    return () => { cancelled = true; };
  }, [excel.ok, excel.ok ? excel.fileId : "", excel.ok ? excel.rows : 0, excelDocLoad, live]);

  // ---------------------------------------------------------------- resizable pane

  useEffect(() => {
    const move = (e: PointerEvent) => {
      const start = resize.current;
      if (!start || start.pointerId !== e.pointerId) return;
      setPaneWidth(Math.max(320, Math.min(window.innerWidth * 0.7, start.width + start.x - e.clientX)));
    };
    const stop = (e: PointerEvent) => {
      if (resize.current?.pointerId !== e.pointerId) return;
      resize.current = null;
      setResizing(false);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
    };
  }, []);

  // ---------------------------------------------------------------- navigation guards

  /** Drafts live in open grid editors; leaving the view unmounts them, so ask first. */
  const confirmLeave = useCallback(() => {
    if (!hasUnsavedEdit) return true;
    if (!window.confirm("יש עריכה פתוחה שלא נשמרה. לעבור בכל זאת? השינויים שלא נשמרו יאבדו.\n\nלשמירה: ביטול, ואז Enter בשדה.")) return false;
    setCellErrors({});
    setDirtyCells(new Set());
    return true;
  }, [hasUnsavedEdit]);

  const changeTab = useCallback((next: TabId) => {
    if (next === tab || !confirmLeave()) return;
    setTab(next);
  }, [tab, confirmLeave]);

  const changeFilters = useCallback((next: Filters) => {
    if (!confirmLeave()) return;
    setFilters(next);
  }, [confirmLeave]);

  const changePage = useCallback((next: number) => {
    if (!confirmLeave()) return;
    setPage(next);
  }, [confirmLeave]);

  // ---------------------------------------------------------------- save / approve

  const handleSave = useCallback(async ({ row, field, index, value }: SaveCellRequest) => {
    const key = cellKey(row.key, field.field_name, index);
    setSavingCell(key);
    setCellErrors((prev) => { const next = { ...prev }; delete next[key]; return next; });
    try {
      await saveField(
        row.doc.file_id,
        row.form.form_id,
        row.exam.examination_id,
        field.field_name === "commitment_number" ? row.commitment.commitment_id : null,
        field.field_name,
        value,
        field.version,
        field.field_name === "central_numbers" && index !== undefined
          ? { index, expected_value: field.normalized ?? field.raw ?? "" }
          : undefined
      );
      if (!live()) return;
      try {
        await data.refreshDoc(row.doc.file_id);
      } catch (err) {
        if (live()) setNotice({ tone: "error", text: `הערך נשמר בשרת, אך רענון הנתונים נכשל: ${errorMessage(err)}` });
      }
    } catch (err) {
      if (!live()) return;
      setCellErrors((prev) => ({ ...prev, [key]: errorMessage(err) }));
      if (err instanceof ApiError && err.status === 409) {
        try { await data.refreshDoc(row.doc.file_id); } catch { /* keep the draft and the conflict message */ }
      }
    } finally {
      if (live()) setSavingCell(null);
    }
  }, [data, live]);

  const handleCancel = useCallback((key: CellKey) => {
    setCellErrors((prev) => { const next = { ...prev }; delete next[key]; return next; });
  }, []);

  const handleApprove = useCallback(async (req: FieldApproveRequest): Promise<boolean> => {
    if (!selectedRow) return false;
    const key = cellKey(selectedRow.key, req.field_name, req.index);
    setApproving(true);
    try {
      await approveField(
        selectedRow.doc.file_id, selectedRow.form.form_id, req.examination_id, req.commitment_id,
        req.field_name, req.approval_status, req.normalized, req.version,
        req.index !== undefined ? { index: req.index, expected_value: req.expected_value } : undefined
      );
      if (!live()) return false;
      setCellErrors((prev) => { const next = { ...prev }; delete next[key]; return next; });
      try { await data.refreshDoc(selectedRow.doc.file_id); } catch (err) {
        if (live()) setNotice({ tone: "error", text: `האישור נשמר בשרת, אך רענון הנתונים נכשל: ${errorMessage(err)}` });
      }
      return true;
    } catch (err) {
      if (!live()) return false;
      setCellErrors((prev) => ({ ...prev, [key]: errorMessage(err) }));
      if (err instanceof ApiError && err.status === 409) {
        try { await data.refreshDoc(selectedRow.doc.file_id); } catch { /* keep the message */ }
      }
      return false;
    } finally {
      if (live()) setApproving(false);
    }
  }, [selectedRow, data, live]);

  const rowErrors = useMemo(() => {
    const errors: Partial<Record<FieldName, string>> = {};
    if (!selectedRow) return errors;
    for (const [key, message] of Object.entries(cellErrors)) {
      const [rowKey, field] = key.split("|");
      if (rowKey === selectedRow.key && field) errors[field as FieldName] = message;
    }
    return errors;
  }, [cellErrors, selectedRow]);

  const rowFailedSaves = useMemo(() => {
    const failed: Partial<Record<FieldName, boolean>> = {};
    for (const field of Object.keys(rowErrors)) failed[field as FieldName] = true;
    return failed;
  }, [rowErrors]);

  // ---------------------------------------------------------------- export / upload

  const handleExcel = useCallback(async () => {
    if (!excel.ok || excelCheck.state !== "ok") return;
    setDownloading(true);
    try {
      let blob: Blob;
      try {
        blob = await downloadExcel(excel.fileId);
      } catch (err) {
        const detail = err instanceof ApiError
          ? (err.body as { detail?: { needs_confirmation?: boolean; summary?: ExportSummary } } | null)?.detail
          : undefined;
        if (!detail?.needs_confirmation) throw err;
        const s = detail.summary;
        const message = [
          "הייצוא חלקי. להוריד למרות הבעיות?",
          s ? `שורות: ${s.rows}; שורות עם תאים לא מאושרים: ${s.pending_rows}; תאים כתומים: ${s.orange_cells}.` : "",
          ...(s?.warnings ?? []),
        ].filter(Boolean).join("\n");
        if (!window.confirm(message)) return;
        blob = await downloadExcel(excel.fileId, true);
      }
      if (!live()) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = excelCheck.summary.filename || "labextract.xlsx";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      if (live()) setNotice({ tone: "error", text: `ייצוא Excel נכשל: ${errorMessage(err)}` });
    } finally {
      if (live()) setDownloading(false);
    }
  }, [excel, excelCheck, live]);

  const handleUploaded = useCallback((meta: Parameters<typeof data.addDoc>[0]) => {
    setShowUpload(false);
    data.addDoc(meta);
    if (hasUnsavedEdit) {
      setNotice({ tone: "info", text: `הקובץ ${meta.filename} הועלה. יש עריכה שלא נשמרה, ולכן התצוגה לא הוחלפה.` });
      return;
    }
    setFilters({ ...EMPTY_FILTERS, scope: "latest_file" });
    setTab("review");
    setPane(null);
    setNotice({ tone: "info", text: `הקובץ ${meta.filename} הועלה, והעיבוד התחיל.` });
  }, [data, hasUnsavedEdit]);

  // ---------------------------------------------------------------- render helpers

  const scopeProblems = sel.scopeDocs.filter((d) => d.load.state !== "ready");
  const failedDocs = sel.scopeDocs
    .map(({ meta, load }) => ({ meta, pages: load.state === "ready" ? failedPages(load.forms) : [] }))
    .filter((d) => d.pages.length > 0);
  const excelReason = !excel.ok
    ? excel.reason
    : excelCheck.state === "checking" ? "בודק מול השרת…"
    : excelCheck.state === "mismatch" ? `השרת מדווח ${excelCheck.serverRows} שורות והמסך ${excel.rows}. רענני את הנתונים.`
    : excelCheck.state === "error" ? excelCheck.message
    : "";
  const excelReady = excel.ok && excelCheck.state === "ok";
  const paneStyle = maximized ? Math.min(window.innerWidth - 80, 1200) : paneWidth;
  const readyRowSelected = selectedRow && selectedRow.status.view !== "review";

  return (
    <div className={`v2${resizing ? " v2--resizing" : ""}`}>
      <div className="v2-head">
        <div>
          <p className="v2-head__eyebrow">מרכז סקירה חכם · V2</p>
          <h1 className="v2-head__title">סקירת התחייבויות</h1>
        </div>
        <div className="v2-actions">
          <button type="button" className="btn btn--primary" onClick={() => setShowUpload(true)}>⬆ העלאת קבצים</button>
          <button
            type="button"
            className="btn btn--secondary"
            disabled={sourceForms.length === 0 || USE_MOCK}
            title={USE_MOCK ? "מצב הדגמה: אין קובצי מקור." : `${sourceForms.length} טפסים בבחירה`}
            onClick={() => { setPane({ kind: "forms", index: 0 }); }}
          >
            צפייה בטופסי מקור ({sourceForms.length})
          </button>
          {sourceDownload.ok && !USE_MOCK ? (
            <a className="btn btn--secondary" href={`${BASE_URL}/documents/${sourceDownload.fileId}/file`} download={sourceDownload.filename}>
              הורדת טפסי מקור
            </a>
          ) : (
            <button type="button" className="btn btn--secondary" disabled title={USE_MOCK ? "מצב הדגמה: אין קובצי מקור." : !sourceDownload.ok ? sourceDownload.reason : ""}>
              הורדת טפסי מקור
            </button>
          )}
          <button type="button" className="btn btn--teal" disabled={!excelReady || downloading} title={excelReason} onClick={handleExcel}>
            {downloading ? "מייצא…" : excelReady ? `ייצוא Excel (${excel.ok ? excel.rows : 0})` : "ייצוא Excel"}
          </button>
          <button type="button" className="btn btn--ghost" disabled title="קידום רשומות שהושלמו (Update Data) אינו קיים בשרת. תלוי ב-Backend.">
            עדכון נתונים <span className="v2-dep">Backend</span>
          </button>
        </div>
      </div>

      {(!excelReady && excelReason) && tab !== "forms" && sel.rows.length > 0 && (
        <p className="v2-hint v2-hint--action">Excel: {excelReason}</p>
      )}
      {!sourceDownload.ok && sourceForms.length > 0 && (
        <p className="v2-hint v2-hint--action">הורדת מקור: {sourceDownload.reason}</p>
      )}

      {notice && (
        <div className={`alert ${notice.tone === "error" ? "alert--error" : "alert--info"} v2-notice`} role={notice.tone === "error" ? "alert" : "status"}>
          <span>{notice.text}</span>
          <button type="button" className="btn btn--ghost btn--sm" onClick={() => setNotice(null)} aria-label="סגירת הודעה">✕</button>
        </div>
      )}

      {data.list.state === "error" && (
        <div className="alert alert--error v2-notice" role="alert">
          <span>טעינת רשימת הקבצים נכשלה: {data.list.message}</span>
          <button type="button" className="btn btn--secondary btn--sm" onClick={() => void data.reload()}>נסי שוב</button>
        </div>
      )}

      <FilterBar filters={filters} tab={tab} senders={senders} latestFilename={latest?.meta.filename ?? null} onChange={changeFilters} />

      {scopeProblems.length > 0 && (
        <ul className="v2-docstates" aria-label="קבצים שאינם מוכנים">
          {scopeProblems.map(({ meta, load }) => (
            <li key={meta.file_id} className={`v2-docstate v2-docstate--${load.state}`}>
              <span dir="auto" className="v2-docstate__name">{meta.filename}</span>
              <span className="v2-docstate__state">{DOC_STATE_LABEL[load.state as keyof typeof DOC_STATE_LABEL]}</span>
              {load.state === "error" && <span className="v2-docstate__msg">{load.message}</span>}
              {load.state === "pending" && (
                <button type="button" className="btn btn--teal btn--sm" onClick={() => void data.processDoc(meta.file_id)}>התחלת עיבוד</button>
              )}
              {load.state === "error" && (
                <button type="button" className="btn btn--secondary btn--sm" onClick={() => void data.processDoc(meta.file_id)}>נסי שוב</button>
              )}
              {(load.state === "processing" || load.state === "loading") && <span className="spinner" aria-hidden="true" />}
              {load.state === "processing" && load.total !== undefined && load.total > 1 && (
                <span className="v2-docstate__msg" aria-live="polite">עמוד {Math.min((load.done ?? 0) + 1, load.total)} מתוך {load.total}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {failedDocs.length > 0 && (
        <ul className="v2-docstates" aria-label="עמודים שעיבודם נכשל">
          {failedDocs.map(({ meta, pages }) => (
            <li key={meta.file_id} className="v2-docstate v2-docstate--error">
              <span dir="auto" className="v2-docstate__name">{meta.filename}</span>
              <span className="v2-docstate__state">
                {pages.length === 1 ? `עמוד ${pages[0]} נכשל בעיבוד` : `${pages.length} עמודים נכשלו בעיבוד`}
              </span>
              <button type="button" className="btn btn--secondary btn--sm" onClick={() => void data.retryFailed(meta.file_id, pages)}>נסי שוב</button>
            </li>
          ))}
          {failedDocs.length > 1 && (
            <li className="v2-docstate">
              <button type="button" className="btn btn--teal btn--sm"
                onClick={() => void data.retryAllFailed(failedDocs.map(({ meta, pages }) => ({ fileId: meta.file_id, pages })))}>
                נסי שוב את כל הנכשלים
              </button>
            </li>
          )}
        </ul>
      )}

      <ViewTabs tab={tab} counts={sel.counts} onChange={changeTab} />

      <div className="v2-work" id="v2-panel" role="tabpanel" aria-labelledby={`v2-tab-${tab}`}>
        <div className="v2-work__main">
          {data.list.state === "loading" ? (
            <div className="loading-row"><span className="spinner" aria-hidden="true" /><span>טוען נתונים…</span></div>
          ) : data.list.state === "ready" && docs.length === 0 ? (
            <div className="v2-empty">
              <p className="v2-empty__title">עדיין לא הועלו קבצים</p>
              <p>העלי קובץ PDF, JPG או PNG כדי להתחיל.</p>
              <button type="button" className="btn btn--primary" onClick={() => setShowUpload(true)}>העלאת קובץ ראשון</button>
            </div>
          ) : tab === "forms" ? (
            <FormsList
              forms={sel.forms}
              selectedKey={pane?.kind === "forms" ? sourceForms[pane.index]?.key ?? null : null}
              onOpen={(index) => setPane({ kind: "forms", index })}
              onConfirmCount={(record) => {
                void confirmCommitmentCount(record.doc.file_id, record.form.form_id)
                  .then(() => data.refreshDoc(record.doc.file_id))
                  .then(() => { if (live()) setNotice({ tone: "info", text: "מספר ההתחייבויות בטופס אושר." }); })
                  .catch((err) => { if (live()) setNotice({ tone: "error", text: `אישור מספר ההתחייבויות נכשל: ${errorMessage(err)}` }); });
              }}
            />
          ) : (
            <CommitmentGrid
              rows={pageRows}
              total={sel.rows.length}
              page={page}
              pageCount={pageCount}
              onPage={changePage}
              selectedKey={selectedRow?.key ?? null}
              selectedCell={pane?.kind === "row" && pane.field ? cellKey(pane.key, pane.field, pane.index ?? undefined) : null}
              allowEditing={tab === "review"}
              savingCell={savingCell}
              cellErrors={cellErrors}
              onSelectRow={(row) => setPane((p) => (p?.kind === "row" && p.key === row.key ? p : { kind: "row", key: row.key, field: defaultSourceField(row), index: null }))}
              onSelectCell={(row, field, index) => setPane({ kind: "row", key: row.key, field, index: index ?? null })}
              onSave={(req) => void handleSave(req)}
              onCancel={handleCancel}
              onDirty={handleDirty}
            />
          )}
          {tab === "gaps" && sel.counts.gaps === 0 && (
            <p className="v2-hint">אישור "חסר במקור" (CONFIRMED_ABSENT) אינו נתמך עדיין בשרת, ולכן תצוגה זו מציגה רק רשומות שהשרת כבר סימן כך.</p>
          )}
        </div>

        {pane && (selectedRow || pane.kind === "forms") && (
          <>
            {!maximized && (
              <div
                className="review-resize-handle"
                role="separator"
                aria-orientation="vertical"
                aria-label="שינוי רוחב חלונית המקור"
                aria-valuemin={320}
                aria-valuenow={Math.round(paneWidth)}
                tabIndex={0}
                onPointerDown={(e) => {
                  e.preventDefault();
                  // Capture keeps the drag alive when the pointer crosses the PDF <object>.
                  e.currentTarget.setPointerCapture(e.pointerId);
                  resize.current = { pointerId: e.pointerId, x: e.clientX, width: paneWidth };
                  setResizing(true);
                }}
                onKeyDown={(e) => {
                  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
                    e.preventDefault();
                    setPaneWidth((w) => Math.max(320, Math.min(window.innerWidth * 0.7, w + (e.key === "ArrowLeft" ? 24 : -24))));
                  }
                }}
              />
            )}
            <div className="v2-pane" style={{ width: paneStyle }}>
              <div className="v2-pane__bar">
                <button type="button" className="btn btn--ghost btn--sm" onClick={() => setMaximized((m) => !m)} aria-pressed={maximized}>
                  {maximized ? "הקטנה" : "הגדלה"}
                </button>
                {readyRowSelected && (
                  <button type="button" className="btn btn--ghost btn--sm" disabled title="החזרה לבדיקה (Reopen) דורשת מעבר מצב בשרת. תלוי ב-Backend.">
                    החזרה לבדיקה <span className="v2-dep">Backend</span>
                  </button>
                )}
                {pane.kind === "forms" && (
                  <button type="button" className="btn btn--ghost btn--sm" onClick={() => setPane(null)} aria-label="סגירת חלונית">✕</button>
                )}
              </div>
              {pane.kind === "row" && selectedRow && (
                <CentralApproval
                  row={selectedRow}
                  busy={savingCell !== null || approving}
                  blocked={selectedRow.status.reasons.some((r) => r.startsWith("בעיה חוסמת"))}
                  errors={cellErrors}
                  onApprove={handleApprove}
                />
              )}
              {pane.kind === "row" && selectedRow ? (
                <SourceViewer
                  key={selectedRow.key}
                  fileId={selectedRow.doc.file_id}
                  filename={selectedRow.doc.filename}
                  pageCount={selectedRow.doc.page_count}
                  panelWidth={paneStyle}
                  selectedFieldName={pane.field}
                  selectedFieldIndex={pane.index}
                  onSelectField={(field, index) => setPane({ kind: "row", key: selectedRow.key, field, index: index ?? null })}
                  exam={selectedRow.exam}
                  commitment={selectedRow.commitment}
                  saving={savingCell !== null || approving}
                  approvalBlocked={selectedRow.status.reasons.some((r) => r.startsWith("בעיה חוסמת"))}
                  fieldErrors={rowErrors}
                  failedSaves={rowFailedSaves}
                  onClose={() => setPane(null)}
                  onFieldApprove={handleApprove}
                />
              ) : pane.kind === "forms" ? (
                <FormSource forms={sourceForms} index={Math.min(pane.index, Math.max(0, sourceForms.length - 1))} onIndex={(index) => setPane({ kind: "forms", index })} />
              ) : null}
            </div>
          </>
        )}
      </div>

      {showUpload && <UploadModal onClose={() => setShowUpload(false)} onUploaded={handleUploaded} />}
    </div>
  );
}
