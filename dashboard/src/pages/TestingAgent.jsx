import React, { useCallback, useEffect, useState } from "react";

import {
    fetchKiteStatus,
    fetchLatency,
    fetchNearMisses,
    fetchPaperTrades,
    getAdminToken,
    setAdminToken
} from "../lib/api.js";

/**
 * Testing Agent — everything being TRIED, kept away from the alerts view.
 *
 * The alerts page is the thing that works and is watched daily. This is where
 * the unproven parts live: what the trading gate would have bought, whether a
 * broker session is live, which alerts were nearly missed, and where the time
 * goes. Separating them means experimenting here cannot disturb that.
 *
 * Nothing on this page can place an order. There is no execution code in the
 * repo at all — see engine/trader.py.
 */

const REFRESH_MS = 30_000;

function Panel({ title, subtitle, children, right }) {
    return (
        <section className="rounded-2xl border border-brand-border bg-brand-dark/40 p-5">
            <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h2 className="font-display text-lg font-semibold text-brand-light">
                        {title}
                    </h2>
                    {subtitle && (
                        <p className="mt-0.5 text-xs text-brand-textMuted">{subtitle}</p>
                    )}
                </div>
                {right}
            </div>
            {children}
        </section>
    );
}

function TokenGate({ token, onChange }) {
    const [value, setValue] = useState(token);

    return (
        <Panel
            title="Admin token"
            subtitle="These views touch the broker session and the trading record, so they are not public. The token is stored in this browser only."
        >
            <div className="flex flex-wrap gap-2">
                <input
                    type="password"
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    placeholder="ALERT_ADMIN_TOKEN"
                    className="min-w-[260px] flex-1 rounded-lg border border-brand-border bg-brand-dark/60 px-3 py-2 font-mono text-sm text-brand-light placeholder:text-brand-textMuted focus:border-brand-cyan/50 focus:outline-none"
                />
                <button
                    onClick={() => onChange(value.trim())}
                    className="rounded-lg border border-brand-cyan/40 px-4 py-2 text-sm text-brand-cyan hover:bg-brand-cyan/10"
                >
                    Save
                </button>
                {token && (
                    <button
                        onClick={() => {
                            setValue("");
                            onChange("");
                        }}
                        className="rounded-lg border border-brand-border px-4 py-2 text-sm text-brand-textMuted hover:text-brand-slate"
                    >
                        Clear
                    </button>
                )}
            </div>
        </Panel>
    );
}

function ShadowBadge() {
    return (
        <span className="rounded-full border border-band-strong/40 bg-band-strong/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-band-strong">
            Shadow · nothing is bought
        </span>
    );
}

function Trades({ data }) {
    if (!data) return <div className="skeleton h-24 rounded-xl" />;
    const taken = (data.trades || []).filter((t) => t.would_trade);
    const skipped = (data.trades || []).filter((t) => !t.would_trade);

    return (
        <>
            <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                {[
                    ["Judged", data.count],
                    ["Would buy", data.would_trade],
                    ["Min order", `₹${data.gate?.min_order_cr} Cr`],
                    ["Min % of m-cap", `${data.gate?.min_order_to_mcap_pct}%`]
                ].map(([label, value]) => (
                    <div
                        key={label}
                        className="rounded-xl border border-brand-border bg-brand-bg2/50 px-3 py-2"
                    >
                        <div className="text-[10px] uppercase tracking-wider text-brand-textMuted">
                            {label}
                        </div>
                        <div className="mt-0.5 font-mono text-base text-brand-light">
                            {value ?? "—"}
                        </div>
                    </div>
                ))}
            </div>

            {taken.length === 0 ? (
                <p className="rounded-xl border border-dashed border-brand-border py-8 text-center text-sm text-brand-textMuted">
                    Nothing has cleared the gate yet. It admits roughly two a week, so
                    quiet stretches are expected.
                </p>
            ) : (
                <div className="overflow-x-auto">
                    <table className="w-full text-left text-sm">
                        <thead className="text-[10px] uppercase tracking-wider text-brand-textMuted">
                            <tr>
                                {["Symbol", "Order", "M-cap", "% of co.", "Qty", "Ref price", "Session"].map(
                                    (h) => (
                                        <th key={h} className="pb-2 pr-4 font-medium">
                                            {h}
                                        </th>
                                    )
                                )}
                            </tr>
                        </thead>
                        <tbody className="font-mono text-brand-slate">
                            {taken.map((t) => (
                                <tr key={t.id} className="border-t border-brand-border/60">
                                    <td className="py-2 pr-4 text-brand-light">
                                        {t.company_symbol}
                                    </td>
                                    <td className="py-2 pr-4">₹{t.order_cr} Cr</td>
                                    <td className="py-2 pr-4">₹{t.market_cap_cr} Cr</td>
                                    <td className="py-2 pr-4 text-brand-cyan">
                                        {t.order_to_mcap_pct}%
                                    </td>
                                    <td className="py-2 pr-4">{t.quantity ?? "—"}</td>
                                    <td className="py-2 pr-4">{t.reference_price ?? "—"}</td>
                                    <td className="py-2 pr-4 text-xs">
                                        {t.session_state === "OPEN" ? (
                                            <span className="text-band-strong">open</span>
                                        ) : (
                                            <span
                                                className="text-brand-textMuted"
                                                title="Filed outside market hours — a real order would wait for the next open"
                                            >
                                                {(t.session_state || "").toLowerCase()}
                                            </span>
                                        )}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}

            {skipped.length > 0 && (
                <details className="mt-4">
                    <summary className="cursor-pointer text-xs text-brand-textMuted hover:text-brand-slate">
                        {skipped.length} alert{skipped.length === 1 ? "" : "s"} the gate
                        turned down — and why
                    </summary>
                    <ul className="mt-2 space-y-1">
                        {skipped.slice(0, 40).map((t) => (
                            <li key={t.id} className="text-xs text-brand-textMuted">
                                <span className="font-mono text-brand-slate">
                                    {t.company_symbol}
                                </span>{" "}
                                · {t.reason}
                            </li>
                        ))}
                    </ul>
                </details>
            )}
        </>
    );
}

function Broker({ status, error }) {
    if (error) {
        return (
            <p className="text-sm text-brand-textMuted">
                {error.includes("401") || error.includes("403")
                    ? "Admin token missing or wrong."
                    : error.includes("503")
                      ? "Disabled — ALERT_ADMIN_TOKEN is not set on the server."
                      : error}
            </p>
        );
    }
    if (!status) return <div className="skeleton h-16 rounded-xl" />;

    if (!status.configured) {
        return (
            <p className="text-sm text-brand-textMuted">
                Kite is not configured, and nothing here needs it. The shadow record
                builds without it.
            </p>
        );
    }
    return (
        <div className="space-y-2 text-sm">
            <div className="flex items-center gap-2">
                <span
                    className={`h-2 w-2 rounded-full ${
                        status.live ? "bg-band-strong" : "bg-brand-textMuted"
                    }`}
                />
                <span className="text-brand-light">
                    {status.live ? `Session live · ${status.user_id}` : "No session"}
                </span>
            </div>
            <p className="text-xs text-brand-textMuted">
                {status.live
                    ? `Expires ${status.expires_at?.slice(0, 16).replace("T", " ")} — tokens die at 06:00 IST and cannot be refreshed.`
                    : status.reason}
            </p>
            {!status.live && status.login_url && (
                <a
                    href={status.login_url}
                    className="inline-block rounded-lg border border-brand-cyan/40 px-3 py-1.5 text-xs text-brand-cyan hover:bg-brand-cyan/10"
                >
                    Log in to Kite
                </a>
            )}
        </div>
    );
}

export default function TestingAgent() {
    const [token, setToken] = useState(getAdminToken());
    const [trades, setTrades] = useState(null);
    const [kite, setKite] = useState(null);
    const [misses, setMisses] = useState(null);
    const [latency, setLatency] = useState(null);
    const [error, setError] = useState(null);

    const saveToken = (v) => {
        setAdminToken(v);
        setToken(v);
    };

    const load = useCallback(async () => {
        // The two public panels load regardless — they are useful even without
        // a token, and failing the whole page over one gated call would hide
        // them for no reason.
        fetchNearMisses({ days: 7, min_value_cr: 50 }).then(setMisses).catch(() => {});
        fetchLatency({ days: 7 }).then(setLatency).catch(() => {});

        if (!token) return;
        try {
            const [t, k] = await Promise.all([
                fetchPaperTrades({ days: 30 }),
                fetchKiteStatus().catch(() => null)
            ]);
            setTrades(t);
            setKite(k);
            setError(null);
        } catch (e) {
            setError(e.message);
        }
    }, [token]);

    useEffect(() => {
        load();
    }, [load]);

    useEffect(() => {
        const id = setInterval(load, REFRESH_MS);
        return () => clearInterval(id);
    }, [load]);

    return (
        <main className="mx-auto max-w-7xl space-y-5 px-5 py-6">
            <div className="rounded-2xl border border-brand-cyan/25 bg-brand-cyan/[0.04] p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                        <h2 className="font-display text-base font-semibold text-brand-light">
                            Testing agent
                        </h2>
                        <p className="mt-0.5 text-xs leading-relaxed text-brand-textMuted">
                            Everything being tried, kept away from the alerts view so
                            experimenting here cannot disturb it. Nothing on this page can
                            place an order — there is no execution code in the engine.
                        </p>
                    </div>
                    <ShadowBadge />
                </div>
            </div>

            {!token && <TokenGate token={token} onChange={saveToken} />}

            <Panel
                title="What it would have bought"
                subtitle="STRONG conviction, order ≥ ₹100 Cr, and worth ≥ 10% of market cap. Recorded, never executed."
                right={
                    token && (
                        <button
                            onClick={() => saveToken("")}
                            className="text-xs text-brand-textMuted hover:text-brand-slate"
                        >
                            Change token
                        </button>
                    )
                }
            >
                {!token ? (
                    <p className="text-sm text-brand-textMuted">
                        Enter the admin token above to see the trading record.
                    </p>
                ) : error ? (
                    <p className="text-sm text-band-watch">{error}</p>
                ) : (
                    <Trades data={trades} />
                )}
            </Panel>

            <div className="grid gap-5 lg:grid-cols-2">
                <Panel title="Broker session" subtitle="Read-only. Auth and margins, no trading.">
                    {token ? (
                        <Broker status={kite} error={error} />
                    ) : (
                        <p className="text-sm text-brand-textMuted">Token required.</p>
                    )}
                </Panel>

                <Panel
                    title="Near misses"
                    subtitle="Filings where an order value was read but no alert followed — ₹50 Cr and up."
                >
                    {!misses ? (
                        <div className="skeleton h-20 rounded-xl" />
                    ) : misses.count === 0 ? (
                        <p className="text-sm text-brand-textMuted">
                            Nothing in the last 7 days.
                        </p>
                    ) : (
                        <ul className="space-y-1.5">
                            {misses.near_misses.slice(0, 8).map((m) => (
                                <li key={m.announcement_id} className="text-xs">
                                    <span className="font-mono text-brand-slate">
                                        {m.company_symbol}
                                    </span>{" "}
                                    <span className="text-brand-light">
                                        ₹{m.largest_order_cr} Cr
                                    </span>{" "}
                                    <span className="text-brand-textMuted">
                                        — {m.why_no_alert}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    )}
                </Panel>
            </div>

            <Panel
                title="Delivery time"
                subtitle="Split into the exchange's own publishing delay and ours, because they are fixed differently."
            >
                {!latency ? (
                    <div className="skeleton h-16 rounded-xl" />
                ) : (
                    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                        {[
                            ["Exchange lag p50", latency.exchange_sec?.p50, "theirs"],
                            ["Our analysis p50", latency.analysis_sec?.p50, "ours"],
                            ["Notify queue p50", latency.queue_sec?.p50, "ours"],
                            [
                                "Under 60s",
                                latency.dashboard_total_sec?.under_60s != null
                                    ? `${latency.dashboard_total_sec.under_60s}/${latency.dashboard_total_sec.n}`
                                    : null,
                                ""
                            ]
                        ].map(([label, value, whose]) => (
                            <div
                                key={label}
                                className="rounded-xl border border-brand-border bg-brand-bg2/50 px-3 py-2"
                            >
                                <div className="text-[10px] uppercase tracking-wider text-brand-textMuted">
                                    {label}
                                </div>
                                <div className="mt-0.5 font-mono text-base text-brand-light">
                                    {value != null ? (typeof value === "number" ? `${value}s` : value) : "—"}
                                </div>
                                {whose && (
                                    <div className="text-[10px] text-brand-textMuted">{whose}</div>
                                )}
                            </div>
                        ))}
                    </div>
                )}
            </Panel>
        </main>
    );
}
