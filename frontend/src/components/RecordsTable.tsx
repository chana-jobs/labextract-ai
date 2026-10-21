/**
 * RecordsTable — 7-field commitment grid with RTL sticky headers.
 * Columns follow Spec §5 order: fields 1–7, column 1 (first_name) rightmost in RTL.
 * Orange-tinted cells = unapproved/pending per Spec §8.3.
 * Inline corrections are enabled only in the needs-review tab.
 */

import type {
  Commitment,
  Examination,
  FieldName,
  FieldValue,
} from "../types/contracts";
import EditableField from "./EditableField";
import type { FieldSaveRequest } from "./SourceViewer";

// ----------------------------------------------------------------- types

export interface CommitmentRow {
  commitmentId: string;
  exam: Examination;
  commitment: Commitment;
}

interface RecordsTableProps {
  rows: CommitmentRow[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onFieldSelect: (id: string, fieldName: FieldName, index?: number) => void;
  allowEditing: boolean;
  saving: boolean;
  fieldErrors: Partial<Record<FieldName, string>>;
  onFieldSave: (request: FieldSaveRequest) => void;
  onFieldSaveCancel: (fieldName: FieldName) => void;
}

// ----------------------------------------------------------------- field helpers

function isCellPending(fv: FieldValue): boolean {
  return (
    fv.approval_status === "PENDING" &&
    fv.extraction_status !== "MISSING"
  );
}

function isCellMissing(fv: FieldValue): boolean {
  return fv.extraction_status === "MISSING" && fv.approval_status !== "CONFIRMED_ABSENT";
}

function isCellConfirmedAbsent(fv: FieldValue): boolean {
  return fv.approval_status === "CONFIRMED_ABSENT";
}

function cellClassName(fv: FieldValue): string {
  if (isCellConfirmedAbsent(fv)) return "cell--confirmed-absent";
  if (isCellMissing(fv) || isCellPending(fv)) return "cell--pending";
  return "";
}

const FIELD_LABELS: Record<FieldName, string> = {
  first_name: "שם פרטי",
  last_name: "שם משפחה",
  israeli_id: "ת.ז. / מזהה",
  central_numbers: "מספר מרכזי",
  sample_received_date: "תאריך קבלת דגימה",
  sender: "שולח",
  commitment_number: "מספר התחייבות",
};

function savedValue(fv: FieldValue): string {
  if (fv.approval_status === "CONFIRMED_ABSENT" ||
      fv.extraction_status === "MISSING" || fv.partial) return "";
  return fv.normalized ?? fv.raw ?? "";
}

function suggestion(fv: FieldValue): string {
  if (fv.extraction_status !== "MISSING" && !fv.partial) return "";
  return fv.normalized ?? fv.raw ?? fv.candidates[0]?.normalized ?? fv.candidates[0]?.raw ?? "";
}

interface FieldCellProps {
  fv: FieldValue;
  onSelect: () => void;
  examinationId: string;
  commitmentId: string | null;
  allowEditing: boolean;
  saving: boolean;
  saveError?: string;
  index?: number;
  onSave: (request: FieldSaveRequest) => void;
  onCancelSave: (fieldName: FieldName) => void;
}

function FieldCell({
  fv,
  onSelect,
  examinationId,
  commitmentId,
  allowEditing,
  saving,
  saveError,
  index,
  onSave,
  onCancelSave,
}: FieldCellProps) {
  const pending = isCellPending(fv);
  const missing = isCellMissing(fv);
  const absent = isCellConfirmedAbsent(fv);
  const val = savedValue(fv);
  const placeholder = suggestion(fv) || (missing ? "השלימי ערך" : "—");

  const hasFlag =
    fv.flags.includes("ID_CHECKSUM_WARNING") ||
    fv.flags.includes("DATE_AMBIGUOUS") ||
    fv.flags.includes("SENDER_UNMAPPED");

  let flagTitle = "";
  if (fv.flags.includes("ID_CHECKSUM_WARNING")) flagTitle = "ספרת ביקורת שגויה";
  else if (fv.flags.includes("DATE_AMBIGUOUS")) flagTitle = "תאריך דו-משמעי (DD/MM)";
  else if (fv.flags.includes("SENDER_UNMAPPED")) flagTitle = "שולח לא ממופה";

  const handleSave = (normalized: string) => onSave({
    examination_id: examinationId,
    commitment_id: fv.field_name === "commitment_number" ? commitmentId : null,
    field_name: fv.field_name,
    normalized,
    version: fv.version,
    ...(fv.field_name === "central_numbers" && index !== undefined
      ? { index, expected_value: fv.normalized ?? fv.raw ?? "" }
      : {}),
  });

  return (
    <div className="field-cell-content">
      <button
        type="button"
        className="field-cell-select"
        onClick={(event) => {
          event.stopPropagation();
          onSelect();
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") event.stopPropagation();
        }}
        aria-label={`הצג מקור עבור ${FIELD_LABELS[fv.field_name]}`}
      >
      <span className={`field-cell${pending ? " field-cell--pending" : ""}`}>
        {absent ? (
          <span className="field-value field-value--empty">—</span>
        ) : missing ? (
          <span className="field-value field-value--suggestion" title={placeholder}>
            {placeholder}
          </span>
        ) : fv.partial ? (
          <span className="field-value field-value--suggestion" title={placeholder}>
            {placeholder || "חלקי — יש להשלים"}
          </span>
        ) : (
          <span className="field-value" title={val}>
            {val || <span className="field-value field-value--empty">—</span>}
          </span>
        )}

        {hasFlag && (
          <span
            className="field-flag field-flag--warn"
            title={flagTitle}
            aria-label={flagTitle}
            role="img"
          >
            !
          </span>
        )}
      </span>
      </button>
      {allowEditing && fv.approval_status !== "CONFIRMED_ABSENT" && (
        <EditableField
          fieldLabel={FIELD_LABELS[fv.field_name]}
          value={val}
          placeholder={placeholder}
          saving={saving}
          saveError={saveError}
          disabled={saving}
          showValue={false}
          onBeforeSave={(previous, next) => window.confirm(
            `לשמור את השינוי בשדה ${FIELD_LABELS[fv.field_name]}?\n\nהערך הקודם: ${previous}\nהערך החדש: ${next}`
          )}
          onEditStart={onSelect}
          onSave={handleSave}
          onCancel={() => onCancelSave(fv.field_name)}
        />
      )}
      {saveError && !allowEditing && <span className="editable-field__error" role="alert">{saveError}</span>}
    </div>
  );
}

// ----------------------------------------------------------------- status badge

function commitmentStatusBadge(exam: Examination, c: Commitment) {
  const fields: FieldValue[] = [
    exam.first_name, exam.last_name, exam.israeli_id,
    exam.sample_received_date, exam.sender, c.number,
    ...exam.central_numbers,
  ];

  if (fields.length === 0) {
    return <span className="badge badge--needs-review">דורשת בדיקה</span>;
  }

  const allResolved = fields.every(
    (f) =>
      f.approval_status === "HUMAN_APPROVED" ||
      f.approval_status === "AUTO_APPROVED" ||
      f.approval_status === "CONFIRMED_ABSENT"
  );

  if (allResolved) {
    const hasAbsent = fields.some((f) => f.approval_status === "CONFIRMED_ABSENT");
    return hasAbsent
      ? <span className="badge badge--ready-gaps">מוכנה (עם חסרים)</span>
      : <span className="badge badge--ready">מוכנה</span>;
  }
  return <span className="badge badge--needs-review">דורשת בדיקה</span>;
}

// ----------------------------------------------------------------- component

export default function RecordsTable({
  rows,
  selectedId,
  onSelect,
  onFieldSelect,
  allowEditing,
  saving,
  fieldErrors,
  onFieldSave,
  onFieldSaveCancel,
}: RecordsTableProps) {
  if (rows.length === 0) {
    return (
      <div className="empty-state">
        <p className="empty-state__title">אין התחייבויות להצגה</p>
        <p className="empty-state__body">אין התחייבויות התואמות את הסינון הנוכחי.</p>
      </div>
    );
  }

  return (
    <div className="records-table-wrap" role="region" aria-label="טבלת התחייבויות">
      <table className="records-table" aria-label="רשימת התחייבויות">
        <thead>
          <tr>
            {/* RTL: column 1 (שם פרטי) is rightmost — browser renders RTL table naturally */}
            <th scope="col">שם פרטי</th>
            <th scope="col">שם משפחה</th>
            <th scope="col">ת.ז. / מזהה</th>
            <th scope="col">מספרים מרכזיים</th>
            <th scope="col">תאריך קבלת דגימה</th>
            <th scope="col">שולח</th>
            <th scope="col">מס׳ התחייבות</th>
            <th scope="col">סטטוס</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ commitmentId, exam, commitment }) => {
            const isSelected = commitmentId === selectedId;
            const centralPending = exam.central_numbers.length === 0 ||
              exam.central_numbers.some((f) => isCellPending(f) || isCellMissing(f));
            const fieldCell = (fv: FieldValue, index?: number) => (
              <FieldCell
                key={`${fv.field_name}-${index ?? "single"}`}
                fv={fv}
                examinationId={exam.examination_id}
                commitmentId={commitmentId}
                index={index}
                onSelect={() => onFieldSelect(commitmentId, fv.field_name, index)}
                allowEditing={allowEditing}
                saving={saving}
                saveError={fieldErrors[fv.field_name]}
                onSave={onFieldSave}
                onCancelSave={onFieldSaveCancel}
              />
            );

            return (
              <tr
                key={commitmentId}
                className={isSelected ? "row--selected" : ""}
                onClick={() => onSelect(commitmentId)}
                aria-selected={isSelected}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") onSelect(commitmentId);
                }}
              >
                <td className={cellClassName(exam.first_name)}>
                  {fieldCell(exam.first_name)}
                </td>
                <td className={cellClassName(exam.last_name)}>
                  {fieldCell(exam.last_name)}
                </td>
                <td className={cellClassName(exam.israeli_id)}>
                  {fieldCell(exam.israeli_id)}
                </td>
                <td className={centralPending ? "cell--pending" : ""}>
                  <div className="field-cell-list">
                    {exam.central_numbers.length > 0
                      ? exam.central_numbers.map((fv, index) => fieldCell(fv, index))
                      : <span className="field-value field-value--empty">—</span>}
                  </div>
                </td>
                <td className={cellClassName(exam.sample_received_date)}>
                  {fieldCell(exam.sample_received_date)}
                </td>
                <td className={cellClassName(exam.sender)}>
                  {fieldCell(exam.sender)}
                </td>
                <td className={cellClassName(commitment.number)}>
                  {fieldCell(commitment.number)}
                </td>
                <td>
                  {commitmentStatusBadge(exam, commitment)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
