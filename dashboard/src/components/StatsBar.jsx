import React from "react";

function Stat({ label, value, accent = "text-brand-light", sub }) {
    return (
        <div className="flex-1 min-w-[130px] rounded-xl border border-brand-border bg-brand-dark/70 px-4 py-3">
            <div className="text-[11px] uppercase tracking-wider text-brand-textMuted">
                {label}
            </div>
            <div className={`mt-1 font-display text-2xl leading-none ${accent}`}>
                {value ?? "—"}
            </div>
            {sub && <div className="mt-1 text-[11px] text-brand-textMuted">{sub}</div>}
        </div>
    );
}

export default function StatsBar({ stats, loading }) {
    if (loading) {
        return (
            <div className="flex flex-wrap gap-3">
                {Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} className="skeleton h-[76px] flex-1 min-w-[130px]" />
                ))}
            </div>
        );
    }

    const a = stats?.alerts || {};
    const p = stats?.pipeline || {};

    return (
        <div className="flex flex-wrap gap-3">
            <Stat
                label="Alerts"
                value={a.total_alerts}
                accent="text-brand-cyan"
                sub={`across ${a.companies ?? 0} ${a.companies === 1 ? "company" : "companies"}`}
            />
            <Stat label="Strong" value={a.strong} accent="text-band-strong" sub="score ≥ 70" />
            <Stat label="Moderate" value={a.moderate} accent="text-band-moderate" sub="score 45–69" />
            <Stat label="Watch" value={a.watch} accent="text-band-watch" sub="score 20–44" />
            <Stat
                label="Filings read"
                value={p.analyzed}
                sub={`${p.skipped ?? 0} skipped · ${p.failed ?? 0} failed`}
            />
        </div>
    );
}
