/**
 * LabExtract AI — FormGrid
 * Renders extracted commitments in a spreadsheet-like grid.
 *
 * Columns follow Spec §5: field order 1–7, column 1 (first_name) is rightmost in RTL.
 * Orange cell = unapproved/missing value (Spec §8.3).
 * Badges reflect CommitmentStatus per Spec §7.4.
 *
 * Task 1 scope: read-only display. Editing is Task 2+.
 */

import type { Commitment, EngineResult, Examination, FieldValue } from "../types/contracts";

// ----------------------------------------------------------------- helpers

function cellClass(fv: FieldValue): string {
  if (
    fv.approval_status === "HUMAN_APPROVED" ||
    fv.approval_status === "AUTO_APPROVED"
  ) {
    return "";
  }
  if (fv.approval_status === "CONFIRMED_ABSENT") {
    return ""; // no orange per Spec §8.3
  }
  return "cell--orange";
}

function displayValue(fv: FieldValue): string {
  if (fv.approval_status === "CONFIRMED_ABSENT") return "—";
  if (fv.normalized) return fv.normalized;
  if (fv.raw) return fv.raw + (fv.partial ? "…" : "");
  return "";
}

function statusLabel(exam: Examination, c: Commitment): { label: string; cls: string } {
  // Simplified client-side status for display only (authoritative status comes from server)
  const fields: FieldValue[] = [
    exam.first_name,
    exam.last_name,
    exam.israeli_id,
    exam.sample_received_date,
    exam.sender,
    c.number,
    ...exam.central_numbers,
  ];
  const allApproved = fields.every(
    (f) =>
      f.approval_status === "HUMAN_APPROVED" ||
      f.approval_status === "AUTO_APPROVED" ||
      f.approval_status === "CONFIRMED_ABSENT"
  );
  if (allApproved) {
    const hasAbsent = fields.some((f) => f.approval_status === "CONFIRMED_ABSENT");
    return hasAbsent
      ? { label: "מוכן (עם חסרים)", cls: "badge badge--needs-review" }
      : { label: "מוכן", cls: "badge badge--ready" };
  }
  return { label: "דורש בדיקה", cls: "badge badge--needs-review" };
}

// ----------------------------------------------------------------- component

interface FormGridProps {
  engineResult: EngineResult;
}

export default function FormGrid({ engineResult }: FormGridProps) {
  const { form, engine_run } = engineResult;

  if (form.processing_status === "FAILED") {
    return (
      <p role="alert" style={{ color: "red", marginTop: "1.5rem" }}>
        עיבוד הטופס נכשל (engine run: {engine_run.run_id}).
      </p>
    );
  }

  const rows: Array<{ exam: Examination; commitment: Commitment }> =
    form.examinations.flatMap((exam) =>
      exam.commitments.map((c) => ({ exam, commitment: c }))
    );

  if (rows.length === 0) {
    return (
      <p style={{ marginTop: "1.5rem" }}>לא נמצאו מחויבויות בטופס זה.</p>
    );
  }

  return (
    <div style={{ marginTop: "2rem" }} aria-label="טבלת מחויבויות">
      <h3 style={{ marginBottom: "0.5rem" }}>
        מחויבויות — טופס {form.form_id}
        {engine_run.mode === "FULL" ? "" : " (מנוע: מצב מקומי בלבד)"}
      </h3>

      {form.issues.length > 0 && (
        <ul
          role="list"
          aria-label="בעיות בטופס"
          style={{
            background: "#fff3cd",
            border: "1px solid #ffc107",
            borderRadius: "6px",
            padding: "0.75rem 1.25rem",
            marginBottom: "1rem",
          }}
        >
          {form.issues.map((issue) => (
            <li key={issue.issue_id}>
              <strong>{issue.reason}</strong>
              {issue.note ? `: ${issue.note}` : ""}
            </li>
          ))}
        </ul>
      )}

      <div style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              {/* RTL: column 1 (first_name) is rightmost — table renders RTL naturally */}
              <th scope="col">שם פרטי</th>
              <th scope="col">שם משפחה</th>
              <th scope="col">ת.ז. / מזהה</th>
              <th scope="col">מספרים מרכזיים</th>
              <th scope="col">תאריך קבלת דגימה</th>
              <th scope="col">שולח</th>
              <th scope="col">מספר מחויבות</th>
              <th scope="col">סטטוס</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ exam, commitment }) => {
              const { label, cls } = statusLabel(exam, commitment);
              const centralDisplay =
                exam.central_numbers.length > 0
                  ? exam.central_numbers.map(displayValue).join("; ")
                  : "";
              const centralClass =
                exam.central_numbers.length === 0 ||
                exam.central_numbers.some((f) => cellClass(f) === "cell--orange")
                  ? "cell--orange"
                  : "";

              return (
                <tr key={commitment.commitment_id}>
                  <td className={cellClass(exam.first_name)}>
                    {displayValue(exam.first_name)}
                  </td>
                  <td className={cellClass(exam.last_name)}>
                    {displayValue(exam.last_name)}
                  </td>
                  <td className={cellClass(exam.israeli_id)}>
                    {displayValue(exam.israeli_id)}
                    {exam.israeli_id.flags.includes("ID_CHECKSUM_WARNING") && (
                      <span
                        title="אזהרת ספרת ביקורת"
                        style={{ marginRight: "0.25rem", color: "#b45309" }}
                        aria-label="אזהרת ספרת ביקורת"
                      >
                        ⚠
                      </span>
                    )}
                  </td>
                  <td className={centralClass}>{centralDisplay}</td>
                  <td className={cellClass(exam.sample_received_date)}>
                    {displayValue(exam.sample_received_date)}
                    {exam.sample_received_date.flags.includes("DATE_AMBIGUOUS") && (
                      <span
                        title="תאריך דו-משמעי (DD/MM)"
                        style={{ marginRight: "0.25rem", color: "#b45309" }}
                        aria-label="תאריך דו-משמעי"
                      >
                        ⚠
                      </span>
                    )}
                  </td>
                  <td className={cellClass(exam.sender)}>
                    {displayValue(exam.sender)}
                    {exam.sender.flags.includes("SENDER_UNMAPPED") && (
                      <span
                        title="שולח לא ממופה בשפת הסגר"
                        style={{ marginRight: "0.25rem", color: "#b45309" }}
                        aria-label="שולח לא ממופה"
                      >
                        ⚠
                      </span>
                    )}
                  </td>
                  <td className={cellClass(commitment.number)}>
                    {displayValue(commitment.number)}
                  </td>
                  <td>
                    <span className={cls}>{label}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
