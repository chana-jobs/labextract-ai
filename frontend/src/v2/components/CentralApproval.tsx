/**
 * Approve central numbers one by one through the existing approval endpoint
 * (target: central_numbers + index + expected_value). SourceViewer shows them
 * read-only, so without this a row could never be completed from V2.
 * Eligibility mirrors backend/fields.py _not_approvable_reason; the server decides.
 */

import type { FieldValue } from "../../types/contracts";
import type { FieldApproveRequest } from "../../components/SourceViewer";
import { cellKey, type CellKey, type CommitmentRowV2 } from "../model";

interface CentralApprovalProps {
  row: CommitmentRowV2;
  busy: boolean;
  blocked: boolean;
  errors: Record<CellKey, string>;
  onApprove: (req: FieldApproveRequest) => Promise<boolean>;
}

function notApprovableReason(fv: FieldValue): string | null {
  if (fv.approval_status !== "PENDING") return null;
  if (fv.extraction_status === "CONFLICT" || fv.uncertainty_reason.includes("CANDIDATE_CONFLICT")) return "סתירה בין קריאות";
  if (fv.partial) return "ערך חלקי";
  if (!fv.normalized) return "אין ערך שמור";
  if (fv.flags.includes("FORMAT_INVALID")) return "פורמט לא תקין";
  return null;
}

const STATUS: Record<FieldValue["approval_status"], string> = {
  PENDING: "ממתין לאישור",
  AUTO_APPROVED: "אומת אוטומטית",
  HUMAN_APPROVED: "אושר ידנית",
  CONFIRMED_ABSENT: "חסר במקור (אושר)",
};

export default function CentralApproval({ row, busy, blocked, errors, onApprove }: CentralApprovalProps) {
  const centrals = row.exam.central_numbers;
  if (centrals.length === 0) return null;
  return (
    <section className="v2-central" aria-label="אישור מספרים מרכזיים">
      <p className="v2-central__title">מספרים מרכזיים</p>
      <ul className="v2-central__list">
        {centrals.map((fv, index) => {
          const reason = notApprovableReason(fv);
          const pending = fv.approval_status === "PENDING";
          const error = errors[cellKey(row.key, "central_numbers", index)];
          return (
            <li key={index} className="v2-central__item">
              <span className="v2-central__value" dir="ltr">{fv.normalized ?? fv.raw ?? "—"}</span>
              <span className={`v2-central__status${pending ? " v2-central__status--pending" : ""}`}>{STATUS[fv.approval_status]}</span>
              {pending && (
                <button
                  type="button"
                  className="sv-action-btn sv-action-btn--approve"
                  disabled={busy || blocked || !!reason}
                  title={blocked ? "אישור חסום עקב בעיה פתוחה בטופס" : reason ?? "אשר ערך שמור"}
                  aria-label={`אשר מספר מרכזי ${fv.normalized ?? ""}`}
                  onClick={() => void onApprove({
                    examination_id: row.exam.examination_id,
                    commitment_id: null,
                    field_name: "central_numbers",
                    approval_status: "HUMAN_APPROVED",
                    normalized: fv.normalized ?? "",
                    version: fv.version,
                    index,
                    expected_value: fv.normalized ?? "",
                  })}
                >
                  ✓
                </button>
              )}
              {pending && reason && <span className="v2-hint">{reason}</span>}
              {error && <span className="editable-field__error" role="alert">{error}</span>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
