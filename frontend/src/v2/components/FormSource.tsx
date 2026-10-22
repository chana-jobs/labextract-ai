/**
 * Form-by-form source navigation over the current selection (all pages of the
 * result set, not only the visible grid page). Shows the original file at the
 * page the server recorded for the form; no position is invented.
 */

import type { FormRecord } from "../model";

const USE_MOCK = import.meta.env.VITE_USE_MOCK === "true";
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "/api";

interface FormSourceProps {
  forms: FormRecord[];
  index: number;
  onIndex: (index: number) => void;
}

export default function FormSource({ forms, index, onIndex }: FormSourceProps) {
  const record = forms[index];
  if (!record) return <p className="v2-hint">אין טפסים בבחירה.</p>;

  const url = `${BASE_URL}/documents/${record.doc.file_id}/file`;
  const ext = record.doc.filename.split(".").pop()?.toLowerCase();
  const page = record.page ?? 1;

  return (
    <div className="v2-formsrc">
      <div className="v2-formsrc__nav">
        <button type="button" className="btn btn--ghost btn--sm" disabled={index <= 0} onClick={() => onIndex(index - 1)} aria-label="טופס קודם">›</button>
        <span aria-live="polite">טופס {index + 1} מתוך {forms.length}</span>
        <button type="button" className="btn btn--ghost btn--sm" disabled={index >= forms.length - 1} onClick={() => onIndex(index + 1)} aria-label="טופס הבא">‹</button>
      </div>
      <p className="v2-formsrc__meta">
        <span dir="auto">{record.doc.filename}</span> · טופס <span dir="ltr">{record.form.form_id}</span>
        {record.page === null ? " · השרת לא רשם עמוד לטופס; מוצג עמוד 1" : ` · עמוד ${page}`}
      </p>
      {record.reasons.length > 0 && (
        <ul className="v2-formsrc__reasons">{record.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      )}
      {USE_MOCK ? (
        <div className="alert alert--warn" role="status">מצב הדגמה: אין קובצי מקור בשרת.</div>
      ) : ext === "pdf" ? (
        <object key={`${url}#${page}`} data={`${url}#page=${page}`} type="application/pdf" className="v2-formsrc__frame" aria-label={`${record.doc.filename}, עמוד ${page}`}>
          <p className="v2-hint">הדפדפן אינו מציג PDF ישירות.</p>
        </object>
      ) : ext === "jpg" || ext === "jpeg" || ext === "png" ? (
        <img src={url} alt={`מסמך מקור ${record.doc.filename}`} className="v2-formsrc__image" />
      ) : (
        <p className="v2-hint">סוג קובץ לא נתמך להצגה.</p>
      )}
      <p className="v2-hint">המציג מציג את הקובץ המקורי במלואו בעמוד הטופס; בידוד טופס בודד דורש Backend.</p>
    </div>
  );
}
