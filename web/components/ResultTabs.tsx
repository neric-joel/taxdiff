"use client";

export type ResultTab = "impact" | "notes" | "audit";

// short: the words on a phone, where the three tabs share one 375 px row.
const TABS: { key: ResultTab; label: string; short?: string }[] = [
  { key: "impact", label: "Impact list" },
  { key: "notes", label: "Notes and letters", short: "Notes" },
  { key: "audit", label: "Audit log" },
];

/** The impact list, the notes and letters, and the audit log under the result, one at a time. Each panel keeps
 * its section and heading (the selected tab shows the heading's words); the panels stay in the page while hidden.
 * Keys as in the side panel's tabs: arrows, Home and End move and select. */
export function ResultTabs({ tab, onTab, counts, panels }: {
  tab: ResultTab; onTab: (tab: ResultTab) => void; counts: Partial<Record<ResultTab, number>>; panels: Record<ResultTab, React.ReactNode>;
}) {
  const onKey = (event: React.KeyboardEvent) => {
    const index = TABS.findIndex((item) => item.key === tab);
    const to = event.key === "ArrowRight" ? (index + 1) % TABS.length
      : event.key === "ArrowLeft" ? (index + TABS.length - 1) % TABS.length
      : event.key === "Home" ? 0
      : event.key === "End" ? TABS.length - 1
      : -1;
    if (to < 0) return;
    event.preventDefault();
    onTab(TABS[to].key);
    document.getElementById(`tab-${TABS[to].key}`)?.focus();
  };
  return (
    <div className="panel result-tabs">
      <div className="tabs" role="tablist" aria-label="Result details">
        {TABS.map(({ key, label, short }) => (
          <button key={key} type="button" role="tab" id={`tab-${key}`} aria-selected={tab === key} aria-controls={`pane-${key}`}
                  tabIndex={tab === key ? 0 : -1} onClick={() => onTab(key)} onKeyDown={onKey}>
            {short ? <><span className="on-wide">{label}</span><span className="on-phone">{short}</span></> : label}
            {counts[key] ? <span className="tab-count num"> {counts[key]}</span> : null}
          </button>
        ))}
      </div>
      {TABS.map(({ key }) => (
        <div key={key} role="tabpanel" id={`pane-${key}`} aria-labelledby={`tab-${key}`} hidden={tab !== key}>{panels[key]}</div>
      ))}
    </div>
  );
}
