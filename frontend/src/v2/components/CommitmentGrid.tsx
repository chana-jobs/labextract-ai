/**
 * Commitment grid: seven fields in spec order (rightmost first in RTL), text
 * statuses (never colour alone). Editing reuses EditableField and is enabled
 * only where the active view allows it; persistence happens in the parent.
 */

import EditableField from "../../components/EditableField";
import type { FieldName, FieldValue } from "../../types/contracts";
import { cellKey, type CellKey, type CommitmentRowV2 } from "../model";

export const FIELD_LABELS: Record<FieldName, string> = {
  first_name: "שם פרטי",
  last_name: "שם משפחה",
  israeli_id: "ת.ז.",
  central_numbers: "מספרים מרכזיים",
  sample_received_date: "תאריך קבלת דגימה",
  sender: "שולח",
  commitment_number: "מס׳ התחייבות",
};

const RTL_FIELDS: FieldName[] = ["first_name", "last_name", "sender"];

export interface SaveCellRequest {
  row: CommitmentRowV2;
  field: FieldValue;
  index?: number;
  value: string;
}

interface CommitmentGridProps {
  rows: CommitmentRowV2[];
  total: number;
  page: number;
  pageCount: number;
  onPage: (page: number) => void;
  selectedKey: string | null;
  selectedCell: CellKey | null;
  allowEditing: boolean;
  savingCell: CellKey | null;
  cellErrors: Record<CellKey, string>;
  onSelectRow: (row: CommitmentRowV2) => void;
  onSelectCell: (row: CommitmentRowV2, field: FieldName, index?: number) => void;
  onSave: (req: SaveCellRequest) => void;
  onCancel: (key: CellKey) => void;
  /** An open editor's draft differs from the saved value (used to guard navigation). */
  onDirty: (key: CellKey, dirty: boolean) => void;
}

/** `empty` = what an empty cell says: unread or conflicting text is never shown as a plain missing value. */
function cellState(fv: FieldValue): { label: string; tone: "ok" | "pending" | "missing" | "absent"; empty: string } {
  if (fv.approval_status === "CONFIRMED_ABSENT") return { label: "חסר במקור (אושר)", tone: "absent", empty: "—" };
  if (fv.approval_status === "AUTO_APPROVED") return { label: "אומת אוטומטית", tone: "ok", empty: "—" };
  if (fv.approval_status === "HUMAN_APPROVED") return { label: "אושר ידנית", tone: "ok", empty: "—" };
  if (fv.extraction_status === "MISSING") return { label: "לא זוהה", tone: "missing", empty: "לא זוהה" };
  if (fv.extraction_status === "CONFLICT") return { label: "סתירה בין קריאות", tone: "pending", empty: "סתירה" };
  if (fv.partial) return { label: "ערך חלקי", tone: "pending", empty: "חלקי" };
  if (fv.extraction_status === "UNCERTAIN") return { label: "קריאה לא ודאית", tone: "pending", empty: "לא קריא" };
  return { label: "ממתין לאישור", tone: "pending", empty: "חסר" };
}

/** Persisted value only; partial/missing values are never shown as if complete. */
function savedValue(fv: FieldValue): string {
  if (fv.approval_status === "CONFIRMED_ABSENT" || fv.extraction_status === "MISSING" || fv.partial) return "";
  return fv.normalized ?? fv.raw ?? "";
}

function StatusBadge({ row }: { row: CommitmentRowV2 }) {
  const { view, origin, reasons, derived } = row.status;
  if (view === "complete") {
    return <span className="v2-badge v2-badge--ok">{origin === "auto" ? "אומתה אוטומטית" : "הושלמה ידנית"}</span>;
  }
  if (view === "gaps") return <span className="v2-badge v2-badge--gap">מוכנה עם חסרים מאושרים</span>;
  return (
    <span className="v2-badge v2-badge--review" title={reasons.join(" · ")}>
      {derived === "FAILED" ? "נכשל" : derived === "AWAITING_EXTERNAL" ? "ממתין לאימות" : "דורשת בדיקה"}
      {reasons[0] && <span className="v2-badge__reason">{reasons[0]}</span>}
    </span>
  );
}

export default function CommitmentGrid(props: CommitmentGridProps) {
  const { rows, total, page, pageCount, onPage, selectedKey, selectedCell, allowEditing, savingCell, cellErrors } = props;

  if (total === 0) {
    return (
      <div className="v2-empty" role="status">
        <p className="v2-empty__title">אין התחייבויות בתצוגה זו</p>
        <p>אין רשומות התואמות את התצוגה ואת הסינון הנוכחיים.</p>
      </div>
    );
  }

  const renderCell = (row: CommitmentRowV2, fv: FieldValue, index?: number) => {
    const key = cellKey(row.key, fv.field_name, index);
    const state = cellState(fv);
    const value = savedValue(fv);
    const hint = !value ? (fv.normalized ?? fv.raw ?? fv.candidates[0]?.normalized ?? fv.candidates[0]?.raw ?? "") : "";
    const error = cellErrors[key];
    const label = FIELD_LABELS[fv.field_name];
    return (
      <div key={key} className={`v2-cell v2-cell--${state.tone}${selectedCell === key ? " v2-cell--selected" : ""}`}>
        <button
          type="button"
          className="v2-cell__value"
          dir={RTL_FIELDS.includes(fv.field_name) ? "rtl" : "ltr"}
          title={`${label}: ${state.label}${fv.flags.length ? ` · ${fv.flags.join(", ")}` : ""}`}
          onClick={(e) => { e.stopPropagation(); props.onSelectCell(row, fv.field_name, index); }}
        >
          {value || <span className="v2-cell__hint">{hint ? `${hint} ?` : state.empty}</span>}
          {fv.flags.length > 0 && <span className="v2-cell__flag" aria-hidden="true">!</span>}
        </button>
        <span className="v2-sr">{state.label}</span>
        {allowEditing && fv.approval_status !== "CONFIRMED_ABSENT" && (
          <EditableField
            fieldLabel={label}
            value={value}
            placeholder={hint || "השלימי ערך"}
            dir={RTL_FIELDS.includes(fv.field_name) ? "rtl" : "ltr"}
            showValue={false}
            saving={savingCell === key}
            saveError={error ?? null}
            disabled={savingCell !== null}
            onBeforeSave={(prev, next) => window.confirm(`לשמור את השינוי בשדה ${label}?\n\nהערך הקודם: ${prev}\nהערך החדש: ${next}`)}
            onEditStart={() => props.onSelectCell(row, fv.field_name, index)}
            commitOnBlur={false}
            onDirtyChange={(dirty) => props.onDirty(key, dirty)}
            onSave={(next) => props.onSave({ row, field: fv, index, value: next })}
            onCancel={() => props.onCancel(key)}
          />
        )}
        {error && !allowEditing && <span className="editable-field__error" role="alert">{error}</span>}
      </div>
    );
  };

  return (
    <div className="v2-grid">
      <div className="v2-grid__scroll" role="region" aria-label="טבלת התחייבויות" tabIndex={0}>
        <table className="v2-table">
          <thead>
            <tr>
              <th scope="col">{FIELD_LABELS.first_name}</th>
              <th scope="col">{FIELD_LABELS.last_name}</th>
              <th scope="col">{FIELD_LABELS.israeli_id}</th>
              <th scope="col">{FIELD_LABELS.central_numbers}</th>
              <th scope="col">{FIELD_LABELS.sample_received_date}</th>
              <th scope="col">{FIELD_LABELS.sender}</th>
              <th scope="col">{FIELD_LABELS.commitment_number}</th>
              <th scope="col">סטטוס</th>
              <th scope="col">מקור</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const { exam, commitment } = row;
              const selected = row.key === selectedKey;
              return (
                <tr
                  key={row.key}
                  className={selected ? "v2-row--selected" : undefined}
                  aria-selected={selected}
                  tabIndex={0}
                  onClick={() => props.onSelectRow(row)}
                  onKeyDown={(e) => {
                    if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
                      e.preventDefault();
                      props.onSelectRow(row);
                    }
                  }}
                >
                  <td>{renderCell(row, exam.first_name)}</td>
                  <td>{renderCell(row, exam.last_name)}</td>
                  <td>{renderCell(row, exam.israeli_id)}</td>
                  <td>
                    {exam.central_numbers.length > 0
                      ? exam.central_numbers.map((fv, i) => renderCell(row, fv, i))
                      : <span className="v2-cell v2-cell--missing"><span className="v2-cell__hint">לא זוהו</span></span>}
                  </td>
                  <td>{renderCell(row, exam.sample_received_date)}</td>
                  <td>{renderCell(row, exam.sender)}</td>
                  <td>{renderCell(row, commitment.number)}</td>
                  <td><StatusBadge row={row} /></td>
                  <td className="v2-table__source" title={`${row.doc.filename} · ${row.form.form_id}`}>
                    <span dir="auto">{row.doc.filename}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="v2-pager" role="navigation" aria-label="עימוד">
        <span>{total} רשומות</span>
        <button type="button" className="btn btn--ghost btn--sm" disabled={page <= 1} onClick={() => onPage(page - 1)} aria-label="עמוד קודם">›</button>
        <span aria-live="polite">עמוד {page} מתוך {pageCount}</span>
        <button type="button" className="btn btn--ghost btn--sm" disabled={page >= pageCount} onClick={() => onPage(page + 1)} aria-label="עמוד הבא">‹</button>
      </div>
    </div>
  );
}
