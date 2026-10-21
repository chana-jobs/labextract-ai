/**
 * SourceViewer — side panel with document viewer + field approval UI.
 *
 * Document viewer:
 *  - Real mode:  fetches the file via GET /documents/:fileId/file and renders
 *    it in an <object> element (PDF) or <img> (JPG/PNG).  No extra libraries.
 *  - Mock mode:  user can load a local file via a file input.  The selected
 *    file is rendered using a local object-URL.  Nothing is uploaded.
 *  - Page navigation for multi-page PDFs uses PDF.js URL hash (#page=N) which
 *    all modern browsers support when PDF is rendered natively.
 *
 * Save / Approve separation (Spec §7.2):
 *  - Inline table corrections call onFieldSave; approval stays PENDING.
 *  - Approve ✓ button → calls onFieldApprove
 *    → only enabled after a SAVED value exists (approval_status !== PENDING is
 *    not set until the server confirms the save).
 *  - "Approve All" approves only fields that are already saved (not PENDING).
 *  - CONFIRMED_ABSENT is disabled until the API contract supports it.
 *
 * Invariants:
 *  - I-3: buttons disabled while parent is saving.
 *  - I-4: saveError shown inline without clearing the field.
 */

import { useState, useCallback, useRef, useEffect } from "react";
import type {
  ApprovalStatus,
  Commitment,
  Evidence,
  Examination,
  FieldName,
  FieldValue,
} from "../types/contracts";

const USE_MOCK = import.meta.env.VITE_USE_MOCK === "true";
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";

// ----------------------------------------------------------------- types

/** Emitted when the user edits a field value (SAVE only, no approval) */
export interface FieldSaveRequest {
  examination_id: string;
  commitment_id: string | null;
  field_name: FieldName;
  normalized: string;
  version: number;
  index?: number;
  expected_value?: string;
}

/** Emitted when the user explicitly approves an already-saved field */
export interface FieldApproveRequest {
  examination_id: string;
  commitment_id: string | null;
  field_name: FieldName;
  approval_status: ApprovalStatus;
  /** Current normalized value (unchanged) */
  normalized: string;
  version: number;
  index?: number;
  expected_value?: string;
}

export interface SourceViewerProps {
  fileId: string;
  filename: string;
  /** Total page count for navigation (null = unknown) */
  pageCount: number | null;
  /** Adjustable width set by the review workspace. */
  panelWidth: number;
  selectedFieldName: FieldName | null;
  selectedFieldIndex: number | null;
  onSelectField: (fieldName: FieldName | null, index?: number | null) => void;
  exam: Examination;
  commitment: Commitment;
  saving?: boolean;
  fieldErrors?: Partial<Record<FieldName, string>>;
  failedSaves?: Partial<Record<FieldName, boolean>>;
  approvalBlocked?: boolean;
  onClose: () => void;
  /** Called when user clicks Approve or Confirm Absent on a saved field */
  onFieldApprove: (req: FieldApproveRequest) => Promise<boolean>;
}

// ----------------------------------------------------------------- label maps

const FIELD_LABELS: Record<string, string> = {
  first_name: "שם פרטי",
  last_name: "שם משפחה",
  israeli_id: "ת.ז. / מזהה",
  central_numbers: "מספרים מרכזיים",
  sample_received_date: "תאריך קבלת דגימה",
  sender: "שולח",
  commitment_number: "מס׳ התחייבות",
};

const APPROVAL_LABELS: Record<ApprovalStatus, string> = {
  PENDING: "ממתין לאישור",
  HUMAN_APPROVED: "אושר",
  AUTO_APPROVED: "אושר אוטומטית",
  CONFIRMED_ABSENT: "אושר כחסר",
};

const EXTRACTION_LABELS: Record<string, string> = {
  COMPLETE: "מלא",
  PARTIAL: "חלקי",
  UNCERTAIN: "לא ודאי",
  CONFLICT: "סתירה",
  MISSING: "לא זוהה",
};

function isValidIsoDate(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  return year > 0 && date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

function isReliableBBox(
  bbox: Evidence["bbox"]
): bbox is [number, number, number, number] {
  return !!bbox &&
    bbox.every(Number.isFinite) &&
    bbox[0] >= 0 &&
    bbox[1] >= 0 &&
    bbox[2] > bbox[0] &&
    bbox[3] > bbox[1];
}

function isFieldApprovable(field: FieldValue): boolean {
  const value = field.normalized ?? "";
  if (field.approval_status !== "PENDING" || field.extraction_status === "MISSING" ||
      field.extraction_status === "CONFLICT" || field.partial || !value ||
      field.flags.includes("FORMAT_INVALID")) return false;
  if (field.field_name === "israeli_id" && !/^\d{9}$/.test(value)) return false;
  if (field.field_name === "sample_received_date" && !isValidIsoDate(value)) return false;
  return true;
}

// ----------------------------------------------------------------- FieldRow

interface FieldRowProps {
  label: string;
  fv: FieldValue;
  examinationId: string;
  commitmentId: string | null;
  saving: boolean;
  approvalBlocked: boolean;
  saveFailed: boolean;
  saveError?: string;
  sourceSelected: boolean;
  onSelectSource: () => void;
  onApprove: (req: FieldApproveRequest) => Promise<boolean>;
}

function FieldRow({
  label,
  fv,
  examinationId,
  commitmentId,
  saving,
  approvalBlocked,
  saveFailed,
  saveError,
  sourceSelected,
  onSelectSource,
  onApprove,
}: FieldRowProps) {
  const isApproved =
    fv.approval_status === "HUMAN_APPROVED" ||
    fv.approval_status === "AUTO_APPROVED";
  const isAbsent = fv.approval_status === "CONFIRMED_ABSENT";
  const isPending = fv.approval_status === "PENDING";
  const isMissing = fv.extraction_status === "MISSING";

  const canApprove = isFieldApprovable(fv) && !approvalBlocked && !saveFailed && !saving;

  const isRtl =
    fv.field_name === "first_name" ||
    fv.field_name === "last_name" ||
    fv.field_name === "sender";

  const displayVal = isAbsent || isMissing || fv.partial ? "" : (fv.normalized ?? fv.raw ?? "");
  const extractionLabel = EXTRACTION_LABELS[fv.extraction_status] ?? fv.extraction_status;
  const approvalLabel = APPROVAL_LABELS[fv.approval_status];

  // --- handlers ---

  const handleApprove = useCallback(() => {
    onApprove({
      examination_id: examinationId,
      commitment_id: commitmentId,
      field_name: fv.field_name,
      approval_status: "HUMAN_APPROVED",
      normalized: fv.normalized ?? fv.raw ?? "",
      version: fv.version,
    });
  }, [onApprove, examinationId, commitmentId, fv]);

  return (
    <div
      className={`sv-field-row${isApproved ? " sv-field-row--approved" : ""}${isAbsent ? " sv-field-row--absent" : ""}`}
    >
      {/* Label + extraction badge */}
      <div className="sv-field-row__header">
        <span className="sv-field-row__label">{label}</span>
        <div className="sv-field-row__header-actions">
          <span
            className={`sv-field-row__extraction-badge sv-field-row__extraction-badge--${fv.extraction_status.toLowerCase()}`}
            title={`זיהוי: ${extractionLabel}`}
          >
            {extractionLabel}
          </span>
          <button
            type="button"
            className="sv-source-select"
            aria-pressed={sourceSelected}
            onClick={onSelectSource}
          >
            {sourceSelected ? "מקור נבחר" : "הצג מקור"}
          </button>
        </div>
      </div>

      {/* Value + approve buttons row */}
      <div className="sv-field-row__value-row">
        <span
          className={`editable-field__value${!displayVal ? " editable-field__value--empty" : ""}`}
          dir={isRtl ? "rtl" : "ltr"}
        >
          {displayVal || (fv.partial ? fv.raw ?? "ערך חלקי" : isMissing ? "לא זוהה" : "—")}
        </span>
        {saveError && <span className="editable-field__error" role="alert">{saveError}</span>}

        <div className="sv-field-row__actions">
          {/* Approve ✓ — only after value is saved */}
          <button
            type="button"
            className={`sv-action-btn sv-action-btn--approve${isApproved ? " sv-action-btn--active" : ""}`}
            onClick={handleApprove}
            disabled={!canApprove}
            aria-pressed={isApproved}
            aria-label={isApproved ? `${label} אושר` : `אשר ${label}`}
            title={
              isApproved
                ? "אושר"
                : canApprove
                ? "אשר ערך שמור"
                : isPending && isMissing
                ? "אין ערך שמור לאישור"
                : "אשר ערך"
            }
          >
            ✓
          </button>

          {/* Confirm absent is not supported by the current API. */}
          <button
            type="button"
            className={`sv-action-btn sv-action-btn--absent${isAbsent ? " sv-action-btn--active" : ""}`}
            disabled
            aria-pressed={isAbsent}
            aria-label="אישור כשדה חסר אינו זמין"
            title="לא נתמך בחוזה השרת"
          >
            ○
          </button>
        </div>
      </div>

      {/* Approval status badge + flags */}
      <div className="sv-field-row__meta">
        <span
          className={`sv-approval-badge sv-approval-badge--${fv.approval_status.toLowerCase()}`}
        >
          {approvalLabel}
        </span>
        {fv.flags.length > 0 && (
          <span
            className="sv-field-flag"
            title={fv.flags.join(", ")}
            aria-label={`אזהרות: ${fv.flags.join(", ")}`}
          >
            ⚠ {fv.flags[0]}
          </span>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------- DocumentViewer

interface DocumentViewerProps {
  fileId: string;
  filename: string;
  pageCount: number | null;
  /** Object-URL for a locally loaded mock file (overrides fetch) */
  localUrl: string | null;
  page: number;
  focusBox: Evidence["bbox"];
  onPageChange: (p: number) => void;
}

function DocumentViewer({
  fileId,
  filename,
  pageCount,
  localUrl,
  page,
  focusBox,
  onPageChange,
}: DocumentViewerProps) {
  const imageViewportRef = useRef<HTMLDivElement>(null);
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 320 });
  const [imageSize, setImageSize] = useState({ width: 0, height: 0 });
  const ext = filename.split(".").pop()?.toLowerCase() ?? "pdf";
  const isPdf = ext === "pdf";
  const isImage = ext === "jpg" || ext === "jpeg" || ext === "png";
  const maxPage = pageCount ?? 1;

  // Real URL from backend; only used when not in mock mode (or no localUrl)
  const remoteUrl = `${BASE_URL}/documents/${fileId}/file`;
  // For PDFs: embed page number via URL hash (supported by browser PDF viewer)
  const pdfSrc = `${localUrl ?? remoteUrl}#page=${page}`;

  const canPrev = page > 1;
  const canNext = page < maxPage;
  const imageBox =
    !isPdf &&
    focusBox &&
    imageSize.width > 0 &&
    imageSize.height > 0 &&
    focusBox[2] <= imageSize.width &&
    focusBox[3] <= imageSize.height
      ? focusBox
      : null;
  const imageFocusStyle = (() => {
    if (!imageBox || viewportSize.width <= 0 || viewportSize.height <= 0) return null;
    const marginX = (imageBox[2] - imageBox[0]) * 0.2;
    const marginY = (imageBox[3] - imageBox[1]) * 0.2;
    const x0 = Math.max(0, imageBox[0] - marginX);
    const y0 = Math.max(0, imageBox[1] - marginY);
    const x1 = Math.min(imageSize.width, imageBox[2] + marginX);
    const y1 = Math.min(imageSize.height, imageBox[3] + marginY);
    const scale = Math.min(
      viewportSize.width / (x1 - x0),
      viewportSize.height / (y1 - y0)
    );
    return {
      width: imageSize.width * scale,
      height: imageSize.height * scale,
      left: (viewportSize.width - (x1 - x0) * scale) / 2 - x0 * scale,
      top: (viewportSize.height - (y1 - y0) * scale) / 2 - y0 * scale,
    };
  })();

  useEffect(() => {
    const viewport = imageViewportRef.current;
    if (!viewport) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) {
        setViewportSize({
          width: entry.contentRect.width,
          height: entry.contentRect.height,
        });
      }
    });
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="doc-viewer">
      {/* Navigation bar */}
      <div className="doc-viewer__nav">
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => onPageChange(page - 1)}
          disabled={!canPrev}
          aria-label="עמוד קודם"
          title="עמוד קודם"
        >
          ›
        </button>
        <span className="doc-viewer__page-label" aria-live="polite">
          עמוד {page} / {maxPage}
        </span>
        <button
          type="button"
          className="btn btn--ghost btn--sm"
          onClick={() => onPageChange(page + 1)}
          disabled={!canNext}
          aria-label="עמוד הבא"
          title="עמוד הבא"
        >
          ‹
        </button>
      </div>

      {/* Document frame */}
      {isPdf ? (
        <object
          key={pdfSrc} // force re-render when page changes
          data={pdfSrc}
          type="application/pdf"
          className="doc-viewer__frame"
          aria-label={`מסמך: ${filename}, עמוד ${page}`}
        >
          <div className="doc-viewer__fallback">
            <p>הדפדפן אינו תומך בהצגת PDF ישירה.</p>
            <a href={pdfSrc} download={filename} className="btn btn--secondary btn--sm">
              הורד PDF
            </a>
          </div>
        </object>
      ) : isImage ? (
        <div ref={imageViewportRef} className="doc-viewer__image-viewport">
          <img
            src={localUrl ?? remoteUrl}
            alt={`מסמך: ${filename}`}
            className={`doc-viewer__image${imageFocusStyle ? " doc-viewer__image--focused" : ""}`}
            style={imageFocusStyle ?? undefined}
            onLoad={(event) => setImageSize({
              width: event.currentTarget.naturalWidth,
              height: event.currentTarget.naturalHeight,
            })}
          />
        </div>
      ) : (
        <div className="doc-viewer__fallback">
          <p>סוג קובץ לא נתמך להצגה: .{ext}</p>
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------------------- MockFileLoader

interface MockFileLoaderProps {
  onFileLoaded: (url: string, filename: string, pageCount: number | null) => void;
  onClear: () => void;
  hasFile: boolean;
  filename: string;
}

function MockFileLoader({ onFileLoaded, onClear, hasFile, filename }: MockFileLoaderProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (!file) return;
      const url = URL.createObjectURL(file);
      const isPdf = file.name.toLowerCase().endsWith(".pdf");
      // For images pageCount = 1; for PDFs we can't know without PDF.js
      onFileLoaded(url, file.name, isPdf ? null : 1);
      e.target.value = "";
    },
    [onFileLoaded]
  );

  return (
    <div className="mock-file-loader">
      <input
        ref={inputRef}
        type="file"
        accept=".pdf,.jpg,.jpeg,.png"
        style={{ display: "none" }}
        onChange={handleChange}
        aria-label="בחר קובץ מקומי להצגה"
      />
      {hasFile ? (
        <div className="mock-file-loader__loaded">
          <span className="mock-file-loader__name" title={filename}>
            📄 {filename}
          </span>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={onClear}
            aria-label="הסר קובץ"
          >
            ✕
          </button>
        </div>
      ) : (
        <div className="mock-file-loader__prompt">
          <span className="mock-file-loader__label">
            [MOCK] בחר קובץ מקומי לתצוגה — לא יועלה לשרת
          </span>
          <button
            type="button"
            className="btn btn--secondary btn--sm"
            onClick={() => inputRef.current?.click()}
            aria-label="בחר קובץ PDF/JPG/PNG"
          >
            בחר קובץ
          </button>
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------------------- SourceViewer (main)

export default function SourceViewer({
  fileId,
  filename,
  pageCount,
  panelWidth,
  selectedFieldName,
  selectedFieldIndex,
  onSelectField,
  exam,
  commitment,
  saving = false,
  fieldErrors = {},
  failedSaves = {},
  onClose,
  onFieldApprove,
  approvalBlocked = false,
}: SourceViewerProps) {
  const [liveMsg, setLiveMsg] = useState("");
  const [currentPage, setCurrentPage] = useState(1);
  const fields: { label: string; fv: FieldValue; cid: string | null }[] = [
    { label: FIELD_LABELS["first_name"]!, fv: exam.first_name, cid: null },
    { label: FIELD_LABELS["last_name"]!, fv: exam.last_name, cid: null },
    { label: FIELD_LABELS["israeli_id"]!, fv: exam.israeli_id, cid: null },
    { label: FIELD_LABELS["sample_received_date"]!, fv: exam.sample_received_date, cid: null },
    { label: FIELD_LABELS["sender"]!, fv: exam.sender, cid: null },
    { label: FIELD_LABELS["commitment_number"]!, fv: commitment.number, cid: commitment.commitment_id },
  ];

  // Mock local file state
  const [localFileUrl, setLocalFileUrl] = useState<string | null>(null);
  const [localFilename, setLocalFilename] = useState<string>("");
  const [localPageCount, setLocalPageCount] = useState<number | null>(null);

  // Revoke object-URL on unmount to avoid memory leak
  useEffect(() => {
    return () => {
      if (localFileUrl) URL.revokeObjectURL(localFileUrl);
    };
  }, [localFileUrl]);

  useEffect(() => {
    setCurrentPage(1);
    setLocalFileUrl(null);
    setLocalFilename("");
    setLocalPageCount(null);
  }, [fileId]);

  const selectedField = selectedFieldName === "central_numbers"
    ? exam.central_numbers[selectedFieldIndex ?? 0]
    : fields.find(({ fv }) => fv.field_name === selectedFieldName)?.fv;
  const hasValidPage = (evidence: Evidence) =>
    Number.isInteger(evidence.page) &&
    evidence.page >= 0 &&
    (pageCount === null || evidence.page < pageCount);
  const selectedEvidence =
    selectedField?.evidence.find((evidence) => hasValidPage(evidence) && isReliableBBox(evidence.bbox)) ??
    selectedField?.evidence.find(hasValidPage);
  const selectedPage = selectedEvidence ? selectedEvidence.page + 1 : null;
  const selectedBox = selectedEvidence && isReliableBBox(selectedEvidence.bbox)
      ? selectedEvidence.bbox
      : null;

  useEffect(() => {
    setCurrentPage(selectedPage ?? 1);
  }, [selectedPage]);

  const handleLocalFileLoaded = useCallback(
    (url: string, name: string, pages: number | null) => {
      if (localFileUrl) URL.revokeObjectURL(localFileUrl);
      setLocalFileUrl(url);
      setLocalFilename(name);
      setLocalPageCount(pages);
      setCurrentPage(1);
    },
    [localFileUrl]
  );

  const handleLocalFileClear = useCallback(() => {
    if (localFileUrl) URL.revokeObjectURL(localFileUrl);
    setLocalFileUrl(null);
    setLocalFilename("");
    setLocalPageCount(null);
    setCurrentPage(1);
  }, [localFileUrl]);

  // Effective page count: local overrides, then backend, then 1
  const effectivePageCount = localPageCount ?? pageCount ?? 1;
  const effectiveFilename = localFileUrl ? localFilename : filename;

  // "Approve All" — only approved fields that are already saved (not PENDING, not MISSING)
  const handleApproveAll = useCallback(async () => {
    const allFields: FieldValue[] = [
      exam.first_name,
      exam.last_name,
      exam.israeli_id,
      exam.sample_received_date,
      exam.sender,
      commitment.number,
    ];

    const approvable = allFields.filter((field) =>
      isFieldApprovable(field) && !failedSaves[field.field_name]
    );

    if (approvable.length === 0 || approvalBlocked) {
      setLiveMsg(approvalBlocked ? "אישור חסום עקב בעיה פתוחה במסמך." : "אין שדות כשירים לאישור.");
      setTimeout(() => setLiveMsg(""), 3000);
      return;
    }

    let approvedCount = 0;
    for (const fv of approvable) {
      const isCmt = fv.field_name === "commitment_number";
      const approved = await onFieldApprove({
        examination_id: exam.examination_id,
        commitment_id: isCmt ? commitment.commitment_id : null,
        field_name: fv.field_name,
        approval_status: "HUMAN_APPROVED",
        normalized: fv.normalized ?? "",
        version: fv.version,
      });
      if (approved) approvedCount += 1;
      else break;
    }

    setLiveMsg(approvedCount === approvable.length
      ? `${approvedCount} שדות אושרו בשרת.`
      : `אושרו ${approvedCount} מתוך ${approvable.length}; היתר נדחו או נכשלו.`);
    setTimeout(() => setLiveMsg(""), 3000);
  }, [exam, commitment, onFieldApprove, approvalBlocked, failedSaves, fieldErrors]);

  // Pending count: fields with saved value but not yet approved
  const pendingCount = [
    exam.first_name,
    exam.last_name,
    exam.israeli_id,
    exam.sample_received_date,
    exam.sender,
    commitment.number,
  ].filter((field) =>
    isFieldApprovable(field) && !failedSaves[field.field_name]
  ).length;

  // Central numbers — read-only (list-level editing future task)
  const centralVal =
    exam.central_numbers
      .map((f) => f.normalized ?? f.raw ?? "")
      .filter(Boolean)
      .join("; ") || "—";
  const centralLabel =
    exam.central_numbers.length > 1
      ? `מספרים מרכזיים (${exam.central_numbers.length})`
      : "מספר מרכזי";

  const showDocViewer = !USE_MOCK || localFileUrl !== null;

  return (
    <aside
      className="source-viewer"
      aria-label="פרטי התחייבות"
      style={{ width: panelWidth, minWidth: panelWidth }}
    >
      {/* Screen-reader live region */}
      <span
        role="status"
        aria-live="polite"
        style={{ position: "absolute", width: 1, height: 1, overflow: "hidden" }}
      >
        {liveMsg}
      </span>

      {/* Header */}
      <div className="source-viewer__header">
        <span className="source-viewer__title">
          התחייבות{" "}
          {commitment.number.normalized ??
            commitment.number.raw ??
            commitment.commitment_id}
        </span>
        <button
          type="button"
          className="source-viewer__close"
          onClick={onClose}
          aria-label="סגור חלונית"
          title="סגור"
        >
          ✕
        </button>
      </div>

      <div className="source-viewer__body">

        {/* ---- Document viewer section ---- */}
        <div>
          <p className="source-viewer__section-title">מקור מסמך</p>

          {USE_MOCK && (
            <MockFileLoader
              hasFile={localFileUrl !== null}
              filename={localFilename}
              onFileLoaded={handleLocalFileLoaded}
              onClear={handleLocalFileClear}
            />
          )}

          {showDocViewer ? (
            <DocumentViewer
              fileId={fileId}
              filename={effectiveFilename}
              pageCount={effectivePageCount}
              localUrl={localFileUrl}
              page={currentPage}
              focusBox={selectedPage === currentPage ? selectedBox : null}
              onPageChange={setCurrentPage}
            />
          ) : (
            <div
              className="source-viewer__unavailable"
              style={{
                minHeight: 80,
                background: "var(--neutral-100)",
                borderRadius: "var(--radius-md)",
              }}
              role="img"
              aria-label="בחרי קובץ מקומי להצגה"
            >
              <span className="source-viewer__unavailable-label">MOCK</span>
              <span className="source-viewer__unavailable-text">
                בחרי קובץ PDF/JPG/PNG מקומי לתצוגה. לא יועלה לשרת.
              </span>
            </div>
          )}
          {selectedField && selectedEvidence && selectedBox && effectiveFilename.toLowerCase().endsWith(".pdf") && (
            <p className="source-viewer__focus-note" role="status">
              מוצג עמוד הראיה. תצוגת PDF המובנית אינה תומכת במיקוד לפי bbox.
            </p>
          )}
          {selectedField && selectedEvidence && !selectedBox && (
            <p className="source-viewer__focus-note" role="status">
              אין מיקום אמין לשדה; מוצג העמוד המלא.
            </p>
          )}
          {selectedField && !selectedEvidence && (
            <p className="source-viewer__focus-note" role="status">
              לא נמצאה ראיה עם מיקום אמין; מוצג העמוד המלא.
            </p>
          )}
          {selectedFieldName !== null && (
            <button
              type="button"
              className="btn btn--ghost btn--sm source-viewer__full-source"
              onClick={() => onSelectField(null, null)}
            >
              תצוגת מקור מלאה
            </button>
          )}
        </div>

        {/* ---- Approval toolbar ---- */}
        <div className="sv-approval-toolbar">
          <span className="sv-approval-toolbar__label">
            {pendingCount > 0
              ? `${pendingCount} שדות שמורים ממתינים לאישור`
              : "כל השדות אושרו"}
          </span>
          <button
            type="button"
            className="btn btn--teal btn--sm"
            onClick={handleApproveAll}
            disabled={saving || pendingCount === 0 || approvalBlocked}
            aria-label="אשר את כל השדות השמורים הממתינים"
          >
            אשר הכול ✓
          </button>
        </div>

        {/* ---- Field rows ---- */}
        <div>
          <p className="source-viewer__section-title">שדות ההתחייבות</p>
          <div className="sv-field-list">
            {fields.map(({ label, fv, cid }) => (
              <FieldRow
                key={fv.field_name}
                label={label}
                fv={fv}
                examinationId={exam.examination_id}
                commitmentId={cid}
                saving={saving}
                approvalBlocked={approvalBlocked}
                saveFailed={!!failedSaves[fv.field_name]}
                saveError={fieldErrors[fv.field_name]}
                sourceSelected={selectedFieldName === fv.field_name}
                onSelectSource={() => onSelectField(fv.field_name, null)}
                onApprove={onFieldApprove}
              />
            ))}

            {/* Central numbers — read-only */}
            <div className="sv-field-row">
              <div className="sv-field-row__header">
                <span className="sv-field-row__label">{centralLabel}</span>
                <span className="sv-field-row__extraction-badge">קריאה בלבד</span>
              </div>
              <div className="sv-field-row__value-row">
                <span className="editable-field__value" dir="ltr" title={centralVal}>
                  {centralVal}
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Issues */}
        {commitment.existence_status === "UNCERTAIN" && (
          <div className="alert alert--warn" role="alert">
            קיום התחייבות זו אינו ודאי — נדרשת בדיקה ידנית.
          </div>
        )}

        {/* Metadata */}
        <div style={{ marginTop: "auto", paddingTop: "0.5rem" }}>
          <p className="source-viewer__section-title">מזהים</p>
          <div className="source-viewer__field-list">
            <div className="source-viewer__field-row">
              <span className="source-viewer__field-label">commitment_id</span>
              <span className="source-viewer__field-value">
                {commitment.commitment_id}
              </span>
            </div>
            <div className="source-viewer__field-row">
              <span className="source-viewer__field-label">examination_id</span>
              <span className="source-viewer__field-value">
                {commitment.examination_id}
              </span>
            </div>
          </div>
        </div>
      </div>
    </aside>
  );
}
