import type { FormRecord } from "../model";

interface FormsListProps {
  forms: FormRecord[];
  selectedKey: string | null;
  onOpen: (index: number) => void;
}

export default function FormsList({ forms, selectedKey, onOpen }: FormsListProps) {
  if (forms.length === 0) {
    return (
      <div className="v2-empty" role="status">
        <p className="v2-empty__title">אין טפסים ללא חילוץ</p>
        <p>השרת לא החזיר טפסים ללא התחייבויות, טפסים שנכשלו או בעיות פתוחות ברמת הטופס בבחירה זו.</p>
      </div>
    );
  }
  return (
    <div className="v2-grid">
      <div className="v2-grid__scroll" role="region" aria-label="טפסים ללא חילוץ" tabIndex={0}>
        <table className="v2-table">
          <thead>
            <tr>
              <th scope="col">קובץ</th>
              <th scope="col">טופס</th>
              <th scope="col">עמוד</th>
              <th scope="col">סטטוס עיבוד</th>
              <th scope="col">סיבה</th>
              <th scope="col">פעולה</th>
            </tr>
          </thead>
          <tbody>
            {forms.map((record, index) => (
              <tr key={record.key} className={record.key === selectedKey ? "v2-row--selected" : undefined} aria-selected={record.key === selectedKey}>
                <td dir="auto">{record.doc.filename}</td>
                <td dir="ltr">{record.form.form_id}</td>
                <td>{record.page ?? "—"}</td>
                <td>{{ QUEUED: "ממתין", RUNNING: "בעיבוד", DONE: "עובד", FAILED: "נכשל" }[record.form.processing_status]}</td>
                <td><ul className="v2-reasons">{record.reasons.map((r) => <li key={r}>{r}</li>)}</ul></td>
                <td>
                  <button type="button" className="btn btn--secondary btn--sm" onClick={() => onOpen(index)}>צפייה במקור</button>
                  <button type="button" className="btn btn--ghost btn--sm" disabled title="פתרון בעיית טופס דורש endpoint בשרת (Backend).">
                    פתרון בעיה <span className="v2-dep">Backend</span>
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
