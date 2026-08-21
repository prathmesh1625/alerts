import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { fetchMarket } from "../lib/api.js";

/**
 * Live NSE prices, refreshed while you watch.
 *
 * The reason this lives here rather than being a link to nseindia.com: NSE
 * shows prices better than we ever will, but it cannot tell you which of
 * today's movers just filed something the screen liked. The `alert` badge is
 * the whole point of the page.
 */

// Matches the server-side cache TTL. Polling faster only re-serves the cache,
// so this is the fastest refresh that actually produces new numbers.
const REFRESH_MS = 20_000;

const COLUMNS = [
    { key: "symbol", label: "Symbol", align: "left" },
    { key: "ltp", label: "LTP", align: "right" },
    { key: "pct_change", label: "% Chg", align: "right" },
    { key: "volume", label: "Volume", align: "right" },
    { key: "turnover_cr", label: "Value (₹ Cr)", align: "right" },
    { key: "high", label: "High", align: "right" },
    { key: "low", label: "Low", align: "right" }
];

const inr = (v, digits = 2) =>
    v === null || v === undefined || Number.isNaN(Number(v))
        ? "—"
        : Number(v).toLocaleString("en-IN", {
              minimumFractionDigits: digits,
              maximumFractionDigits: digits
          });

const intInr = (v) =>
    v === null || v === undefined ? "—" : Number(v).toLocaleString("en-IN");

function MarketStatus({ status, fetchedAt, stale, refreshing }) {
    const open = status?.is_open;
    const pct = status?.pct_change;

    return (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
            <div className="flex items-center gap-2">
                <span
                    className={`h-2 w-2 rounded-full ${
                        open ? "animate-pulse bg-band-strong" : "bg-brand-textMuted"
                    }`}
                />
                <span
                    className={`text-sm font-medium ${
                        open ? "text-band-strong" : "text-brand-textMuted"
                    }`}
                >
                    {open ? "Market open" : "Market closed"}
                </span>
            </div>

            {status?.nifty && (
                <div className="flex items-baseline gap-2">
                    <span className="text-xs uppercase tracking-wider text-brand-textMuted">
                        Nifty 50
                    </span>
                    <span className="font-mono text-lg text-brand-light">
                        {inr(status.nifty)}
                    </span>
                    {pct != null && (
                        <span
                            className={`font-mono text-sm ${
                                Number(pct) >= 0 ? "text-band-strong" : "text-red-400"
                            }`}
                        >
                            {Number(pct) >= 0 ? "+" : ""}
                            {inr(pct)}%
                        </span>
                    )}
                </div>
            )}

            <span className="font-mono text-[11px] text-brand-textMuted">
                {stale
                    ? "NSE unreachable — showing last snapshot"
                    : refreshing
                      ? "updating…"
                      : status?.as_of || fetchedAt || ""}
            </span>
        </div>
    );
}

export default function Market() {
    const [snap, setSnap] = useState(null);
    const [error, setError] = useState(null);
    const [refreshing, setRefreshing] = useState(false);
    const [sort, setSort] = useState({ key: "turnover_cr", dir: "desc" });
    const [query, setQuery] = useState("");
    const [alertsOnly, setAlertsOnly] = useState(false);
    const [flash, setFlash] = useState({});

    const load = useCallback(async () => {
        setRefreshing(true);
        try {
            const next = await fetchMarket();
            // Flash a cell green/red when its price actually moved, so a change
            // is visible without staring at the number.
            setSnap((prev) => {
                if (prev) {
                    const moves = {};
                    const before = new Map(prev.stocks.map((s) => [s.symbol, s.ltp]));
                    next.stocks.forEach((s) => {
                        const old = before.get(s.symbol);
                        if (old != null && s.ltp != null && s.ltp !== old) {
                            moves[s.symbol] = s.ltp > old ? "up" : "down";
                        }
                    });
                    if (Object.keys(moves).length) {
                        setFlash(moves);
                        setTimeout(() => setFlash({}), 1200);
                    }
                }
                return next;
            });
            setError(null);
        } catch (e) {
            setError(e.message);
        } finally {
            setRefreshing(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    useEffect(() => {
        const id = setInterval(load, REFRESH_MS);
        return () => clearInterval(id);
    }, [load]);

    const rows = useMemo(() => {
        let list = snap?.stocks || [];
        const q = query.trim().toLowerCase();
        if (q) list = list.filter((s) => s.symbol.toLowerCase().includes(q));
        if (alertsOnly) list = list.filter((s) => s.alert);

        const { key, dir } = sort;
        return [...list].sort((a, b) => {
            const av = a[key];
            const bv = b[key];
            if (av === bv) return 0;
            if (av === null || av === undefined) return 1;
            if (bv === null || bv === undefined) return -1;
            const cmp = typeof av === "string" ? av.localeCompare(bv) : av - bv;
            return dir === "asc" ? cmp : -cmp;
        });
    }, [snap, query, alertsOnly, sort]);

    const toggleSort = (key) =>
        setSort((s) =>
            s.key === key
                ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
                : { key, dir: key === "symbol" ? "asc" : "desc" }
        );

    return (
        <main className="mx-auto max-w-7xl space-y-4 px-5 py-6">
            <div className="rounded-2xl border border-brand-border bg-brand-dark/70 px-5 py-4">
                <MarketStatus
                    status={snap?.status}
                    fetchedAt={snap?.fetched_at}
                    stale={snap?.stale}
                    refreshing={refreshing}
                />
            </div>

            <div className="flex flex-wrap items-center gap-3">
                <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Filter by symbol…"
                    className="min-w-[160px] flex-1 rounded-lg border border-brand-border bg-brand-dark/60 px-3 py-2 text-sm text-brand-light placeholder:text-brand-textMuted focus:border-brand-cyan/50 focus:outline-none"
                />
                <button
                    onClick={() => setAlertsOnly((v) => !v)}
                    className={`rounded-lg border px-3 py-2 text-xs font-medium transition-colors ${
                        alertsOnly
                            ? "border-brand-cyan/50 bg-brand-cyan/15 text-brand-cyan"
                            : "border-brand-border text-brand-textMuted hover:text-brand-slate"
                    }`}
                >
                    With alerts only
                    {snap?.alerted_symbols ? ` (${snap.alerted_symbols})` : ""}
                </button>
                <button
                    onClick={load}
                    disabled={refreshing}
                    className="rounded-lg border border-brand-border px-3 py-2 text-xs text-brand-slate transition-colors hover:border-brand-cyan/50 hover:text-brand-cyan disabled:opacity-50"
                >
                    {refreshing ? "…" : "Refresh"}
                </button>
            </div>

            {error && (
                <div className="rounded-lg border border-band-watch/40 bg-band-watch/5 px-4 py-3 text-sm text-band-watch">
                    {error}
                </div>
            )}

            <div className="overflow-hidden rounded-2xl border border-brand-border bg-brand-dark/60">
                <div className="overflow-x-auto">
                    <table className="w-full min-w-[760px] text-sm">
                        <thead>
                            <tr className="border-b border-brand-border bg-brand-bg2/60">
                                {COLUMNS.map((c) => (
                                    <th
                                        key={c.key}
                                        onClick={() => toggleSort(c.key)}
                                        className={`cursor-pointer select-none whitespace-nowrap px-4 py-2.5 text-[11px] font-medium uppercase tracking-wider text-brand-textMuted transition-colors hover:text-brand-cyan ${
                                            c.align === "right" ? "text-right" : "text-left"
                                        }`}
                                    >
                                        {c.label}
                                        {sort.key === c.key && (
                                            <span className="ml-1 text-brand-cyan">
                                                {sort.dir === "asc" ? "▲" : "▼"}
                                            </span>
                                        )}
                                    </th>
                                ))}
                            </tr>
                        </thead>
                        <tbody>
                            {rows.map((s) => {
                                const up = Number(s.pct_change) >= 0;
                                const moved = flash[s.symbol];
                                return (
                                    <tr
                                        key={s.symbol}
                                        className={`border-b border-brand-border/40 transition-colors last:border-0 ${
                                            moved === "up"
                                                ? "bg-band-strong/10"
                                                : moved === "down"
                                                  ? "bg-red-500/10"
                                                  : "hover:bg-brand-bg2/40"
                                        }`}
                                    >
                                        <td className="whitespace-nowrap px-4 py-2.5">
                                            <div className="flex items-center gap-2">
                                                <span className="font-medium text-brand-light">
                                                    {s.symbol}
                                                </span>
                                                {s.alert && (
                                                    <Link
                                                        to="/"
                                                        title={s.alert.headline}
                                                        className="rounded border border-band-strong/40 bg-band-strong/10 px-1.5 py-px text-[10px] font-medium text-band-strong hover:bg-band-strong/20"
                                                    >
                                                        alert {Math.round(s.alert.score)}
                                                    </Link>
                                                )}
                                            </div>
                                        </td>
                                        <td className="px-4 py-2.5 text-right font-mono text-brand-light">
                                            {inr(s.ltp)}
                                        </td>
                                        <td
                                            className={`px-4 py-2.5 text-right font-mono ${
                                                up ? "text-band-strong" : "text-red-400"
                                            }`}
                                        >
                                            {up ? "+" : ""}
                                            {inr(s.pct_change)}%
                                        </td>
                                        <td className="px-4 py-2.5 text-right font-mono text-brand-slate">
                                            {intInr(s.volume)}
                                        </td>
                                        <td className="px-4 py-2.5 text-right font-mono text-brand-slate">
                                            {inr(s.turnover_cr, 0)}
                                        </td>
                                        <td className="px-4 py-2.5 text-right font-mono text-brand-textMuted">
                                            {inr(s.high)}
                                        </td>
                                        <td className="px-4 py-2.5 text-right font-mono text-brand-textMuted">
                                            {inr(s.low)}
                                        </td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>

                {!snap && !error && (
                    <div className="space-y-2 p-4">
                        {Array.from({ length: 10 }).map((_, i) => (
                            <div key={i} className="skeleton h-9 rounded" />
                        ))}
                    </div>
                )}
                {snap && rows.length === 0 && (
                    <p className="px-4 py-16 text-center text-sm text-brand-textMuted">
                        No stock matches.
                    </p>
                )}
            </div>

            <p className="text-xs leading-relaxed text-brand-textMuted">
                Live from NSE, refreshed every {REFRESH_MS / 1000}s and cached on the
                server so every viewer costs one upstream request rather than one
                each. <strong className="text-brand-slate">These are the stocks NSE
                is reporting movement in today</strong> — not the full listed
                universe: NSE retired the endpoint behind its complete table, and
                per-symbol quotes are bot-protected. The <em>alert</em> badge marks
                companies that also cleared the filing formula, which is the one
                thing NSE&apos;s own screen cannot tell you.
            </p>
        </main>
    );
}
