/** Truthful Hebrew messages for API failures (no silent fallback). */

import { ApiError } from "../api/client";

export function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { detail?: { error?: string; reason?: string } | string } | null;
    const detail = body?.detail;
    const code = typeof detail === "object" && detail ? detail.reason ?? detail.error : detail;
    switch (err.status) {
      case 401: return "החיבור פג. יש להתחבר מחדש.";
      case 403: return "אין הרשאה לפעולה זו.";
      case 404: return "הפריט לא נמצא בשרת.";
      case 409: return "הנתונים השתנו בשרת. הנתונים העדכניים נטענו מחדש; השווי ונסי שוב.";
      case 413: return "הקובץ גדול מהמותר.";
      case 422: return code ? `השרת דחה את הפעולה (${code}).` : "השרת דחה את הפעולה.";
      default:
        return err.status >= 500 ? `שגיאת שרת (HTTP ${err.status}). נסי שוב מאוחר יותר.` : `הפעולה נכשלה (HTTP ${err.status}).`;
    }
  }
  if (err instanceof TypeError) return "שגיאת רשת. בדקי את החיבור ונסי שוב.";
  if (err instanceof Error) return err.message;
  return "שגיאה לא ידועה.";
}
