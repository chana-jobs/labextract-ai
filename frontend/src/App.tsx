/**
 * LabExtract AI — AppShell (Task 2)
 * Professional enterprise shell: Navy header, navigation, mock banner.
 * Direction set on <html dir="rtl"> in index.html.
 */

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import ReviewWorkspace from "./pages/ReviewWorkspace";
import {
  ApiError,
  beginAuthSession,
  getSession,
  invalidateAuthSession,
  login,
  logout,
} from "./api/client";

const USE_MOCK = import.meta.env.VITE_USE_MOCK === "true";

type NavSection = "review";
type AuthState = "checking" | "signed-in" | "signed-out";

function authError(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) return "פרטי ההתחברות אינם תקינים או שהחיבור פג.";
  if (error instanceof ApiError && error.status === 429) return "נחסמו ניסיונות התחברות רבים מדי. נסי שוב מאוחר יותר.";
  if (error instanceof ApiError) return `הפעולה נכשלה (HTTP ${error.status}).`;
  return "לא ניתן להתחבר לשרת. בדקי את החיבור ונסי שוב.";
}

export default function App() {
  const [section] = useState<NavSection>("review");
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [sessionKey, setSessionKey] = useState<string | null>(null);
  const authEpoch = useRef(0);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authMessage, setAuthMessage] = useState("");
  const [authBusy, setAuthBusy] = useState(false);

  useEffect(() => {
    let active = true;
    const checkedEpoch = authEpoch.current;
    const onUnauthorized = () => {
      authEpoch.current += 1;
      invalidateAuthSession();
      setSessionKey(null);
      setEmail("");
      setPassword("");
      setAuthBusy(false);
      setAuthMessage("החיבור פג. התחברי מחדש.");
      setAuthState("signed-out");
    };
    window.addEventListener("labextract:unauthorized", onUnauthorized);
    getSession()
      .then((principal) => {
        if (active && authEpoch.current === checkedEpoch) {
          beginAuthSession();
          const sessionId = ++authEpoch.current;
          setSessionKey(`${principal.user_id}:${sessionId}`);
          setAuthState("signed-in");
        }
      })
      .catch(() => {
        if (active && authEpoch.current === checkedEpoch) {
          setSessionKey(null);
          setAuthState("signed-out");
        }
      });
    return () => {
      active = false;
      window.removeEventListener("labextract:unauthorized", onUnauthorized);
    };
  }, []);

  const handleLogin = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAuthBusy(true);
    setAuthMessage("");
    const requestEpoch = ++authEpoch.current;
    let completionEpoch = requestEpoch;
    try {
      const principal = await login(email, password);
      if (authEpoch.current !== requestEpoch) return;
      beginAuthSession();
      completionEpoch = ++authEpoch.current;
      const sessionId = completionEpoch;
      setSessionKey(`${principal.user_id}:${sessionId}`);
      setPassword("");
      setAuthState("signed-in");
    } catch (error: unknown) {
      if (authEpoch.current === requestEpoch) setAuthMessage(authError(error));
    } finally {
      if (authEpoch.current === completionEpoch) setAuthBusy(false);
    }
  }, [email, password]);

  const handleLogout = useCallback(async () => {
    const requestEpoch = ++authEpoch.current;
    invalidateAuthSession();
    setSessionKey(null);
    setAuthState("signed-out");
    setEmail("");
    setPassword("");
    setAuthBusy(true);
    setAuthMessage("");
    try {
      await logout();
    } catch (error: unknown) {
      if (authEpoch.current === requestEpoch) setAuthMessage(authError(error));
    } finally {
      if (authEpoch.current === requestEpoch) setAuthBusy(false);
    }
  }, []);

  return (
    <div className="app-shell">
      {/* Mock indicator — shown only when VITE_USE_MOCK=true */}
      {USE_MOCK && (
        <div className="mock-banner" role="status" aria-live="polite">
          מצב הדגמה — כל הנתונים סינתטיים בלבד. אין נתוני מטופלים אמיתיים.
        </div>
      )}

      {/* Primary header */}
      <header className="app-header" role="banner">
        <div className="app-header__inner">
          <div className="app-header__brand">
            <span className="app-header__logo">
              <img src="/sheba-logo.svg" alt="לוגו המרכז הרפואי שיבא" />
            </span>
            <span className="app-header__brand-copy">
              <span className="app-header__title">LabExtract AI</span>
              <span className="app-header__subtitle">מערכת עיבוד טפסי התחייבויות</span>
            </span>
          </div>

          <nav className="app-header__nav" aria-label="ניווט ראשי">
            <button
              type="button"
              className={`app-header__nav-item${section === "review" ? " app-header__nav-item--active" : ""}`}
              aria-current={section === "review" ? "page" : undefined}
            >
              סקירת התחייבויות
            </button>
          </nav>

          <div className="app-header__meta" aria-label="מידע מערכת">
            <span className="app-header__version">v0.1-demo</span>
            {authState === "signed-in" && (
              <button type="button" className="btn btn--secondary btn--sm" onClick={handleLogout} disabled={authBusy}>
                יציאה
              </button>
            )}
            {USE_MOCK && <span className="app-header__mock">MOCK</span>}
          </div>
        </div>
      </header>

      {/* Main body */}
      <div className="app-body">
        <main className="app-content" id="main-content" tabIndex={-1}>
          {authState === "checking" && <div className="loading-row"><span className="spinner" aria-hidden="true" /><span>בודקת התחברות…</span></div>}
          {authState === "signed-out" && (
            <form className="login-card" onSubmit={handleLogin} aria-labelledby="login-title">
              <span className="login-card__eyebrow">LabExtract AI</span>
              <h1 id="login-title" className="login-card__title">התחברות</h1>
              <p className="login-card__description">התחברי למערכת כדי להמשיך לסקירת המסמכים.</p>
              <label className="login-card__field">
                דוא״ל
                <input className="search-box__input" type="email" autoComplete="username" required value={email} onChange={(event) => setEmail(event.target.value)} />
              </label>
              <label className="login-card__field">
                סיסמה
                <input className="search-box__input" type="password" autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} />
              </label>
              {authMessage && <div className="alert alert--error" role="alert">{authMessage}</div>}
              <button className="btn btn--primary" type="submit" disabled={authBusy}>
                {authBusy ? "מתחברת…" : "התחברות"}
              </button>
            </form>
          )}
          {authState === "signed-in" && sessionKey && (
            <ReviewWorkspace key={sessionKey} />
          )}
        </main>
      </div>
    </div>
  );
}
