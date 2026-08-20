import React from "react";

// 90 is the API's hard ceiling (see the Query bound in api.py). Useful when
// testing against seeded historical filings, which are older than a live feed
// would ever be.
const WINDOWS = [
    { label: "Today", value: 1 },
    { label: "3 days", value: 3 },
    { label: "1 week", value: 7 },
    { label: "1 month", value: 30 },
    { label: "3 months", value: 90 }
];

const BANDS = [
    { label: "All", value: 0 },
    { label: "Watch+", value: 20 },
    { label: "Moderate+", value: 45 },
    { label: "Strong", value: 70 }
];

function Segmented({ options, value, onChange }) {
    return (
        <div className="flex rounded-lg border border-brand-border bg-brand-dark/60 p-0.5">
            {options.map((o) => (
                <button
                    key={o.value}
                    onClick={() => onChange(o.value)}
                    className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                        value === o.value
                            ? "bg-brand-cyan/15 text-brand-cyan"
                            : "text-brand-textMuted hover:text-brand-slate"
                    }`}
                >
                    {o.label}
                </button>
            ))}
        </div>
    );
}

export default function FilterBar({
    days,
    minScore,
    query,
    onDays,
    onMinScore,
    onQuery,
    onRefresh,
    refreshing
}) {
    return (
        <div className="flex flex-wrap items-center gap-3">
            <Segmented options={WINDOWS} value={days} onChange={onDays} />
            <Segmented options={BANDS} value={minScore} onChange={onMinScore} />

            <div className="relative min-w-[160px] flex-1">
                <input
                    value={query}
                    onChange={(e) => onQuery(e.target.value)}
                    placeholder="Filter by symbol or company…"
                    className="w-full rounded-lg border border-brand-border bg-brand-dark/60 px-3 py-2 text-sm text-brand-light placeholder:text-brand-textMuted focus:border-brand-cyan/50 focus:outline-none"
                />
                {query && (
                    <button
                        onClick={() => onQuery("")}
                        className="absolute right-2 top-1/2 -translate-y-1/2 px-1 text-brand-textMuted hover:text-brand-light"
                        aria-label="Clear filter"
                    >
                        ×
                    </button>
                )}
            </div>

            <button
                onClick={onRefresh}
                disabled={refreshing}
                className="rounded-lg border border-brand-border px-3 py-2 text-xs text-brand-slate transition-colors hover:border-brand-cyan/50 hover:text-brand-cyan disabled:opacity-50"
            >
                {refreshing ? "Refreshing…" : "Refresh"}
            </button>
        </div>
    );
}
