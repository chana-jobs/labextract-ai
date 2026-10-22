/**
 * UploadModal — drag-and-drop / browse PDF upload dialog.
 *
 * Spec §2.2 (demo/production): upload is allowed only for authenticated users.
 * In mock mode a synthetic DocumentMeta is returned without real file I/O.
 *
 * Accessibility:
 *  - role="dialog" with aria-modal and aria-labelledby
 *  - Focus trapped inside the modal while open
 *  - Keyboard: Escape closes, Enter/Space activates drop-zone button
 *  - Error messages linked via aria-describedby
 */

import {
  useRef,
  useState,
  useCallback,
  useEffect,
  type DragEvent,
  type ChangeEvent,
  type KeyboardEvent,
} from "react";
import type { DocumentMeta } from "../types/contracts";
import { uploadDocument, ApiError } from "../api/client";

const USE_MOCK = import.meta.env.VITE_USE_MOCK === "true";
const ACCEPTED_TYPES = new Set(["application/pdf", "image/jpeg", "image/png"]);
const ACCEPTED_EXTENSIONS = new Set([".pdf", ".jpg", ".jpeg", ".png"]);
// direct upload to storage (server: LABEXTRACT_MAX_DIRECT_UPLOAD_MB; bucket: migrations/002_direct_upload_size.sql)
const MAX_SIZE_MB = 20;

// ----------------------------------------------------------------- types

interface UploadModalProps {
  /** Called when the modal should close without uploading */
  onClose: () => void;
  /** Called after a successful upload with the new DocumentMeta */
  onUploaded: (doc: DocumentMeta) => void;
}

type UploadState = "idle" | "uploading" | "done" | "error";

// ----------------------------------------------------------------- helpers

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function validateFile(file: File): string | null {
  const extension = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (!ACCEPTED_EXTENSIONS.has(extension) || (file.type && !ACCEPTED_TYPES.has(file.type))) {
    return "ניתן להעלות קובצי PDF, JPG או PNG בלבד.";
  }
  if (file.size > MAX_SIZE_MB * 1024 * 1024) {
    return `גודל הקובץ חורג מהמותר (${MAX_SIZE_MB} MB).`;
  }
  return null;
}

// ----------------------------------------------------------------- component

export default function UploadModal({ onClose, onUploaded }: UploadModalProps) {
  const [dragOver, setDragOver] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploadState, setUploadState] = useState<UploadState>("idle");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const firstFocusRef = useRef<HTMLButtonElement>(null);
  const errorId = "upload-error-msg";
  const titleId = "upload-modal-title";

  // Trap focus: focus first focusable element on open
  useEffect(() => {
    firstFocusRef.current?.focus();
  }, []);

  // Close on Escape
  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  // ---- drag handlers ----

  const handleDragOver = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(true);
  }, []);

  const handleDragLeave = useCallback(() => {
    setDragOver(false);
  }, []);

  const handleDrop = useCallback((e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const err = validateFile(file);
    if (err) { setErrorMsg(err); setSelectedFile(null); return; }
    setErrorMsg(null);
    setSelectedFile(file);
    setUploadState("idle");
  }, []);

  // ---- file input change ----

  const handleFileChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const err = validateFile(file);
    if (err) { setErrorMsg(err); setSelectedFile(null); return; }
    setErrorMsg(null);
    setSelectedFile(file);
    setUploadState("idle");
    // Reset input so the same file can be re-selected after clearing
    e.target.value = "";
  }, []);

  const handleDropZoneKey = useCallback((e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInputRef.current?.click();
    }
  }, []);

  // ---- upload ----

  const handleUpload = useCallback(async () => {
    if (!selectedFile) return;
    setUploadState("uploading");
    setErrorMsg(null);
    try {
      const doc = await uploadDocument(selectedFile);
      setUploadState("done");
      // Short visual pause so the user sees the "done" state
      setTimeout(() => onUploaded(doc), 600);
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setErrorMsg(`שגיאת שרת (HTTP ${err.status})`);
      } else if (err instanceof Error) {
        setErrorMsg(err.message);
      } else {
        setErrorMsg("שגיאה לא ידועה בעלייה לשרת.");
      }
      setUploadState("error");
    }
  }, [selectedFile, onUploaded]);

  const handleClear = useCallback(() => {
    setSelectedFile(null);
    setUploadState("idle");
    setErrorMsg(null);
  }, []);

  // ---- backdrop click ----

  const handleBackdropClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose]
  );

  // ----------------------------------------------------------------- render

  const isUploading = uploadState === "uploading";
  const isDone = uploadState === "done";

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onClick={handleBackdropClick}
      aria-hidden="false"
    >
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={errorMsg ? errorId : undefined}
      >
        {/* Header */}
        <div className="modal__header">
          <h2 className="modal__title" id={titleId}>
            העלאת מסמך חדש
          </h2>
          <button
            ref={firstFocusRef}
            type="button"
            className="modal__close"
            onClick={onClose}
            aria-label="סגור חלון העלאה"
            title="סגור"
          >
            ✕
          </button>
        </div>

        {/* Body */}
        <div className="modal__body">
          {USE_MOCK && (
            <div className="alert alert--warn" role="status" style={{ marginBottom: "0.75rem" }}>
              מצב Mock — לא יועלה קובץ אמיתי. ייווצר מסמך סינתטי לדוגמה.
            </div>
          )}

          {/* Drop zone */}
          <div
            className={`upload-dropzone${dragOver ? " upload-dropzone--active" : ""}${selectedFile ? " upload-dropzone--has-file" : ""}`}
            role="button"
            tabIndex={selectedFile ? -1 : 0}
            aria-label="גרור קובץ PDF, JPG או PNG לכאן, או לחצי לבחירת קובץ"
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            onKeyDown={handleDropZoneKey}
            onClick={() => !selectedFile && fileInputRef.current?.click()}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png"
              style={{ display: "none" }}
              onChange={handleFileChange}
              aria-hidden="true"
            />

            {selectedFile ? (
              <div className="upload-dropzone__file-info">
                <span className="upload-dropzone__file-icon" aria-hidden="true">📄</span>
                <span className="upload-dropzone__filename" title={selectedFile.name}>
                  {selectedFile.name}
                </span>
                <span className="upload-dropzone__filesize">
                  {formatSize(selectedFile.size)}
                </span>
              </div>
            ) : (
              <div className="upload-dropzone__prompt">
                <span className="upload-dropzone__icon" aria-hidden="true">⬆</span>
                <span className="upload-dropzone__text">
                  גרור קובץ PDF, JPG או PNG לכאן
                </span>
                <span className="upload-dropzone__or">או</span>
                <span className="upload-dropzone__browse">
                  לחצי לבחירת קובץ
                </span>
                <span className="upload-dropzone__hint">
                  PDF, JPG או PNG · עד {MAX_SIZE_MB} MB
                </span>
              </div>
            )}
          </div>

          {/* Error message */}
          {errorMsg && (
            <div
              id={errorId}
              className="alert alert--error"
              role="alert"
              style={{ marginTop: "0.5rem" }}
            >
              {errorMsg}
            </div>
          )}

          {/* Done */}
          {isDone && (
            <div className="alert" role="status"
              style={{
                background: "var(--color-success-bg)",
                border: "1px solid var(--color-success-border)",
                color: "var(--color-success-text)",
                marginTop: "0.5rem",
              }}
            >
              הקובץ הועלה בהצלחה — המסמך נוסף לרשימה.
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="modal__footer">
          {selectedFile && !isDone && (
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={handleClear}
              disabled={isUploading}
              aria-label="בטל בחירת קובץ"
            >
              בטל
            </button>
          )}

          <div style={{ flex: 1 }} />

          <button
            type="button"
            className="btn btn--ghost"
            onClick={onClose}
            disabled={isUploading}
          >
            סגור
          </button>

          <button
            type="button"
            className="btn btn--primary"
            onClick={handleUpload}
            disabled={!selectedFile || isUploading || isDone}
            aria-busy={isUploading}
          >
            {isUploading ? (
              <>
                <span className="spinner" aria-hidden="true" />
                מעלה…
              </>
            ) : isDone ? (
              "הועלה ✓"
            ) : (
              "העלאה"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
