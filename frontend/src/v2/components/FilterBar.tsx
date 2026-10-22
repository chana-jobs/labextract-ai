import type { Filters, Scope, TabId } from "../model";

interface FilterBarProps {
  filters: Filters;
  tab: TabId;
  senders: string[];
  latestFilename: string | null;
  onChange: (next: Filters) => void;
}

const SCOPES: { id: Scope | "latest_batch"; label: string; disabledReason?: string }[] = [
  { id: "latest_file", label: "קובץ אחרון" },
  { id: "latest_batch", label: "אצווה אחרונה", disabledReason: "השרת אינו מנהל אצוות (Batch). תלוי ב-Backend." },
  { id: "upload_range", label: "טווח תאריכי העלאה" },
  { id: "all", label: "הכול" },
];

export default function FilterBar({ filters, tab, senders, latestFilename, onChange }: FilterBarProps) {
  const set = (patch: Partial<Filters>) => onChange({ ...filters, ...patch });
  const rowFiltersDisabled = tab === "forms";
  const active =
    filters.query || filters.sender || filters.receiptFrom || filters.receiptTo ||
    filters.uploadFrom || filters.uploadTo || filters.scope !== "latest_file";

  return (
    <section className="v2-filters" aria-label="סינון">
      <div className="v2-scope" role="radiogroup" aria-label="היקף נתונים">
        {SCOPES.map((s) => {
          const disabled = !!s.disabledReason;
          const checked = !disabled && filters.scope === s.id;
          return (
            <button
              key={s.id}
              type="button"
              role="radio"
              aria-checked={checked}
              aria-disabled={disabled}
              disabled={disabled}
              title={s.disabledReason ?? (s.id === "latest_file" && latestFilename ? latestFilename : undefined)}
              className={`v2-chip${checked ? " v2-chip--active" : ""}${disabled ? " v2-chip--blocked" : ""}`}
              onClick={() => !disabled && set({ scope: s.id as Scope })}
            >
              {s.label}
              {disabled && <span className="v2-dep" aria-hidden="true">Backend</span>}
            </button>
          );
        })}
        {filters.scope === "latest_file" && latestFilename && (
          <span className="v2-scope__file" dir="auto" title={latestFilename}>{latestFilename}</span>
        )}
      </div>

      <div className="v2-filters__row">
        {filters.scope === "upload_range" && (
          <span className="v2-range" role="group" aria-label="טווח תאריכי העלאה">
            <label>העלאה מ־<input type="date" value={filters.uploadFrom} onChange={(e) => set({ uploadFrom: e.target.value })} /></label>
            <label>עד<input type="date" value={filters.uploadTo} onChange={(e) => set({ uploadTo: e.target.value })} /></label>
          </span>
        )}

        <label className="v2-search">
          <span className="v2-sr">חיפוש</span>
          <input
            type="search"
            placeholder={tab === "forms" ? "חיפוש לפי שם קובץ או מזהה טופס…" : "חיפוש: שם, ת.ז., מס׳ התחייבות, מספר מרכזי…"}
            value={filters.query}
            onChange={(e) => set({ query: e.target.value })}
          />
        </label>

        <label className="v2-field">
          שולח
          <select
            value={filters.sender}
            disabled={rowFiltersDisabled}
            onChange={(e) => set({ sender: e.target.value })}
          >
            <option value="">כל השולחים</option>
            {senders.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>

        <span className="v2-range" role="group" aria-label="טווח תאריכי קבלת דגימה">
          <label>
            קבלת דגימה מ־
            <input type="date" disabled={rowFiltersDisabled} value={filters.receiptFrom} onChange={(e) => set({ receiptFrom: e.target.value })} />
          </label>
          <label>
            עד
            <input type="date" disabled={rowFiltersDisabled} value={filters.receiptTo} onChange={(e) => set({ receiptTo: e.target.value })} />
          </label>
        </span>

        {active && (
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => onChange({ ...filters, query: "", sender: "", receiptFrom: "", receiptTo: "", uploadFrom: "", uploadTo: "", scope: "latest_file" })}
          >
            ניקוי סינון
          </button>
        )}
      </div>
      {(filters.receiptFrom || filters.receiptTo) && !rowFiltersDisabled && (
        <p className="v2-hint">סינון לפי תאריך קבלת דגימה מחריג רשומות שאין להן תאריך תקין.</p>
      )}
    </section>
  );
}
