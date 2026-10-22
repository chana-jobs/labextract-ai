/**
 * Shell for ReviewWorkspaceV2 (separate entry: v2.html), so App.tsx and the
 * old screen stay untouched. Reuses the auth API and session handling from
 * api/client.ts. On logout / session expiry the workspace is unmounted, so no
 * prior-user rows, previews or drafts survive.
 */

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, beginAuthSession, getSession, invalidateAuthSession, login, logout } from "../api/client";
import ReviewWorkspaceV2 from "../pages/ReviewWorkspaceV2";

const USE_MOCK = import.meta.env.VITE_USE_MOCK === "true";

type AuthState = "checking" | "signed-in" | "signed-out";

function authError(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) return "פרטי ההתחברות אינם תקינים או שהחיבור פג.";
  if (error instanceof ApiError && error.status === 429) return "נחסמו ניסיונות התחברות רבים מדי. נסי שוב מאוחר יותר.";
  if (error instanceof ApiError) return `הפעולה נכשלה (HTTP ${error.status}).`;
  return "לא ניתן להתחבר לשרת. בדקי את החיבור ונסי שוב.";
}

export default function AppV2() {
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [sessionKey, setSessionKey] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);

  const signedIn = useCallback((userId: string) => {
    beginAuthSession();
    setSessionKey(`${userId}:${++epoch.current}`);
    setAuthState("signed-in");
  }, []);

  const signedOut = useCallback((text = "") => {
    epoch.current += 1;
    invalidateAuthSession();
    setSessionKey(null);
    setEmail("");
    setPassword("");
    setBusy(false);
    setMessage(text);
    setAuthState("signed-out");
  }, []);

  useEffect(() => {
    let active = true;
    const checked = epoch.current;
    const onUnauthorized = () => signedOut("החיבור פג. התחברי מחדש.");
    window.addEventListener("labextract:unauthorized", onUnauthorized);
    getSession()
      .then((p) => { if (active && epoch.current === checked) signedIn(p.user_id); })
      .catch(() => { if (active && epoch.current === checked) setAuthState("signed-out"); });
    return () => {
      active = false;
      window.removeEventListener("labextract:unauthorized", onUnauthorized);
    };
  }, [signedIn, signedOut]);

  const handleLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    const request = ++epoch.current;
    try {
      const principal = await login(email, password);
      if (epoch.current !== request) return;
      setPassword("");
      signedIn(principal.user_id);
    } catch (error) {
      if (epoch.current === request) setMessage(authError(error));
    } finally {
      setBusy(false);
    }
  };

  const handleLogout = async () => {
    signedOut();
    try { await logout(); } catch (error) { setMessage(authError(error)); }
  };

  return (
    <div className="app-shell">
      {USE_MOCK && (
        <div className="mock-banner" role="status">מצב הדגמה (VITE_USE_MOCK) — כל הנתונים סינתטיים בלבד.</div>
      )}
      <header className="app-header" role="banner">
        <div className="app-header__inner">
          <div className="app-header__brand">
            <span className="app-header__logo"><img src="/sheba-logo.svg" alt="לוגו המרכז הרפואי שיבא" /></span>
            <span className="app-header__brand-copy">
              <span className="app-header__title">LabExtract AI</span>
              <span className="app-header__subtitle">מערכת עיבוד טפסי התחייבויות</span>
            </span>
          </div>
          <nav className="app-header__nav" aria-label="ניווט ראשי">
            <span className="app-header__nav-item app-header__nav-item--active" aria-current="page">סקירת התחייבויות</span>
            <span className="app-header__nav-item" aria-disabled="true" title="היסטוריית אצוות וארכיון דורשים Backend.">ארכיון</span>
          </nav>
          <div className="app-header__meta">
            <span className="app-header__version">v0.2-preview</span>
            {authState === "signed-in" && (
              <button type="button" className="btn btn--secondary btn--sm" onClick={handleLogout}>יציאה</button>
            )}
            {USE_MOCK && <span className="app-header__mock">MOCK</span>}
          </div>
        </div>
      </header>
      <div className="app-body">
        <main className="app-content" id="main-content" tabIndex={-1}>
          {authState === "checking" && <div className="loading-row"><span className="spinner" aria-hidden="true" /><span>בודקת התחברות…</span></div>}
          {authState === "signed-out" && (
            <form className="login-card" onSubmit={handleLogin} aria-labelledby="login-title">
              <span className="login-card__eyebrow">LabExtract AI</span>
              <h1 id="login-title" className="login-card__title">התחברות</h1>
              <label className="login-card__field">
                דוא״ל
                <input className="search-box__input" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
              </label>
              <label className="login-card__field">
                סיסמה
                <input className="search-box__input" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
              </label>
              {message && <div className="alert alert--error" role="alert">{message}</div>}
              <button className="btn btn--primary" type="submit" disabled={busy}>{busy ? "מתחברת…" : "התחברות"}</button>
            </form>
          )}
          {authState === "signed-in" && sessionKey && <ReviewWorkspaceV2 key={sessionKey} />}
        </main>
      </div>
    </div>
  );
}
