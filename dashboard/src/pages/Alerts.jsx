import React, { useCallback, useEffect, useMemo, useState } from "react";

import AlertCard from "../components/AlertCard.jsx";
import FilterBar from "../components/FilterBar.jsx";
import FormulaPanel from "../components/FormulaPanel.jsx";
import StatsBar from "../components/StatsBar.jsx";
import { fetchAlerts, fetchConfig, fetchStats } from "../lib/api.js";

const REFRESH_MS = 60_000;

function EmptyState({ days, minScore, query }) {
    const reason = query
        ? `No alert matches “${query}”.`
        : minScore > 0
          ? "No filing reached that conviction level in this window."
          : "No filing cleared the formula in this window.";

    return (
        <div className="rounded-2xl border border-dashed border-brand-border py-16 text-center">
            <div className="text-3xl opacity-40">◔</div>
            <p className="mt-3 text-brand-slate">{reason}</p>
            <p className="mx-auto mt-1 max-w-md text-sm text-brand-textMuted">
                The agent only raises an alert when a filing clears at least one rule,
                so quiet days are expected — most filings are routine compliance
                documents. Try widening the window beyond {days} day
                {days === 1 ? "" : "s"}.
            </p>
        </div>
    );
}

function ErrorState({ error, onRetry }) {
    return (
        <div className="rounded-2xl border border-band-watch/40 bg-band-watch/5 p-6 text-center">
            <p className="font-medium text-band-watch">Could not reach the alert engine</p>
            <p className="mx-auto mt-1 max-w-lg font-mono text-xs text-brand-slate">
                {error}
            </p>
            <p className="mx-auto mt-2 max-w-lg text-xs text-brand-textMuted">
                Check that the API is running (<code>python api.py</code> in{" "}
                <code>engine/</code>) and that VITE_API_BASE points at it.
            </p>
            <button
                onClick={onRetry}
                className="mt-4 rounded-lg border border-brand-border px-4 py-2 text-xs text-brand-slate hover:border-brand-cyan/50 hover:text-brand-cyan"
            >
                Try again
            </button>
        </div>
    );
}

export default function Alerts({ onUpdated }) {
    const [alerts, setAlerts] = useState([]);
    const [stats, setStats] = useState(null);
    const [config, setConfig] = useState(null);

    const [days, setDays] = useState(3);
    const [minScore, setMinScore] = useState(0);
    const [query, setQuery] = useState("");

    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);
    const [error, setError] = useState(null);
    const [lastUpdated, setLastUpdated] = useState(null);
    const [formulaOpen, setFormulaOpen] = useState(false);

    const load = useCallback(async () => {
        setRefreshing(true);
        try {
            const [alertsRes, statsRes] = await Promise.all([
                fetchAlerts({ days, min_score: minScore }),
                fetchStats({ days })
            ]);
            setAlerts(alertsRes.alerts || []);
            setStats(statsRes);
            setLastUpdated(new Date());
            if (onUpdated) onUpdated(new Date());
            setError(null);
        } catch (e) {
            setError(e.message);
        } finally {
            setLoading(false);
            setRefreshing(false);
        }
    }, [days, minScore]);

    // The formula panel is static config — fetched once, not on every refresh.
    useEffect(() => {
        fetchConfig().then(setConfig).catch(() => {});
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    useEffect(() => {
        const id = setInterval(load, REFRESH_MS);
        return () => clearInterval(id);
    }, [load]);

    // Symbol/name search is client-side: the result set is already capped at a
    // few hundred rows, so a round-trip per keystroke would buy nothing.
    const visible = useMemo(() => {
        const q = query.trim().toLowerCase();
        if (!q) return alerts;
        return alerts.filter(
            (a) =>
                a.company_symbol?.toLowerCase().includes(q) ||
                a.company_name?.toLowerCase().includes(q)
        );
    }, [alerts, query]);

    return (
        <main className="mx-auto max-w-7xl space-y-5 px-5 py-6">
                <StatsBar stats={stats} loading={loading} />

                <FormulaPanel
                    config={config}
                    open={formulaOpen}
                    onToggle={() => setFormulaOpen((v) => !v)}
                />

                <FilterBar
                    days={days}
                    minScore={minScore}
                    query={query}
                    onDays={setDays}
                    onMinScore={setMinScore}
                    onQuery={setQuery}
                    onRefresh={load}
                    refreshing={refreshing}
                />

                {error ? (
                    <ErrorState error={error} onRetry={load} />
                ) : loading ? (
                    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                        {Array.from({ length: 6 }).map((_, i) => (
                            <div key={i} className="skeleton h-64 rounded-2xl" />
                        ))}
                    </div>
                ) : visible.length === 0 ? (
                    <EmptyState days={days} minScore={minScore} query={query} />
                ) : (
                    <>
                        <div className="text-xs text-brand-textMuted">
                            Showing {visible.length} alert
                            {visible.length === 1 ? "" : "s"}
                            {query && ` matching “${query}”`}
                        </div>
                        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                            {visible.map((a) => (
                                <AlertCard key={a.announcement_id} alert={a} />
                            ))}
                        </div>
                    </>
                )}
        </main>
    );
}
