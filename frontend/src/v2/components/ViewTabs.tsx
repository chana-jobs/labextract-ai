import type { TabId } from "../model";

export const TABS: { id: TabId; label: string }[] = [
  { id: "review", label: "דורשות בדיקה" },
  { id: "complete", label: "מוכנות ומאושרות" },
  { id: "gaps", label: "חסרים מאושרים" },
  { id: "all", label: "כל ההתחייבויות" },
  { id: "forms", label: "טפסים ללא חילוץ" },
];

interface ViewTabsProps {
  tab: TabId;
  counts: Record<TabId, number>;
  onChange: (tab: TabId) => void;
}

export default function ViewTabs({ tab, counts, onChange }: ViewTabsProps) {
  return (
    <div className="v2-tabs" role="tablist" aria-label="תצוגות עבודה">
      {TABS.map(({ id, label }) => (
        <button
          key={id}
          type="button"
          role="tab"
          id={`v2-tab-${id}`}
          aria-selected={tab === id}
          aria-controls="v2-panel"
          className={`v2-tab${tab === id ? " v2-tab--active" : ""}`}
          onClick={() => onChange(id)}
        >
          {label}
          <span className={`v2-tab__count${(id === "review" || id === "forms") && counts[id] > 0 ? " v2-tab__count--warn" : ""}`}>
            {counts[id]}
          </span>
        </button>
      ))}
    </div>
  );
}
