/**
 * BatchSummary — compact collapsible KPI bar for a single document batch.
 * Distinguishes document processing status from commitment review status.
 * Read-only display component (Task 2).
 */

import { useState } from "react";
import type { DocumentMeta, ExportSummary } from "../types/contracts";

interface BatchSummaryProps {
  doc: DocumentMeta;
  summary: ExportSummary;
}

const PROC_LABELS: Record<DocumentMeta["processing_status"], string> = {
  QUEUED: "ממתין לעיבוד",
  RUNNING: "בעיבוד",
  DONE: "עיבוד הושלם",
  FAILED: "עיבוד נכשל",
};

export default function BatchSummary({ doc, summary }: BatchSummaryProps) {
  const [open, setOpen] = useState(false);

  const hasWarnings = summary.warnings.length > 0 || summary.is_partial;

  return (
    <div className="batch-summary" aria-label="סיכום אצווה">
      {/* Collapsed header — always visible */}
      <div
        className={`batch-summary__header${open ? " batch-summary__header--open" : ""}`}
        role="button"
        tabIndex={0}
        aria-expanded={open}
        aria-controls="batch-summary-body"
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen((v) => !v);
          }
        }}
      >
        {/* Document name + processing status */}
        <span className="batch-summary__title">{doc.filename}</span>

        {/* KPI pills */}
        <div className="batch-summary__kpis" aria-label="נתוני אצווה">
          {/* Processing status — distinct from commitment review status */}
          <span className="batch-summary__kpi batch-summary__kpi--info">
            {PROC_LABELS[doc.processing_status]}
          </span>

          <span className="batch-summary__kpi batch-summary__kpi--info">
            {summary.rows} התחייבויות
          </span>

          {summary.pending_rows > 0 && (
            <span className="batch-summary__kpi batch-summary__kpi--warn">
              {summary.pending_rows} דורשות בדיקה
            </span>
          )}

          {summary.ready > 0 && (
            <span className="batch-summary__kpi batch-summary__kpi--ok">
              {summary.ready} מוכנות
            </span>
          )}

          {doc.page_count != null && (
            <span className="batch-summary__kpi">
              {doc.page_count} {doc.page_count === 1 ? "עמוד" : "עמודים"}
            </span>
          )}
        </div>

        {/* Chevron — flipped in RTL so it still makes visual sense */}
        <span
          className={`batch-summary__chevron${open ? " batch-summary__chevron--open" : ""}`}
          aria-hidden="true"
        >
          ▾
        </span>
      </div>

      {/* Expanded body */}
      {open && (
        <div className="batch-summary__body" id="batch-summary-body">
          <p
            style={{
              fontSize: "var(--text-xs)",
              color: "var(--neutral-500)",
              marginBottom: "0.5rem",
            }}
          >
            תאריך העלאה:{" "}
            {new Date(doc.uploaded_at ?? doc.upload_date ?? "").toLocaleString("he-IL", {
              dateStyle: "medium",
              timeStyle: "short",
            })}
          </p>

          {hasWarnings ? (
            <div className="batch-summary__warnings" aria-label="אזהרות אצווה">
              {summary.is_partial && (
                <div className="batch-summary__warning-item">
                  ישנם ערכים חלקיים — מספר מחויבות או שדות נוספים לא נקראו במלואם
                </div>
              )}
              {summary.warnings.map((w) => (
                <div key={w} className="batch-summary__warning-item">
                  {w}
                </div>
              ))}
            </div>
          ) : (
            <p
              style={{
                fontSize: "var(--text-xs)",
                color: "var(--color-success-text)",
              }}
            >
              אין אזהרות לאצווה זו.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
