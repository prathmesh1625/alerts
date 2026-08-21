import React, { useEffect, useState } from "react";

import { fetchVolumeAlerts } from "../lib/api.js";
import { bandStyle } from "../lib/format.js";

/**
 * Rule 4 — sudden volume spikes.
 *
 * Rendered as its own section rather than mixed into the filing cards above,
 * because it is a different kind of claim. Those say "this company said
 * something"; this says "this stock suddenly got traded far more than usual",
 * with no filing behind it and a separate score.
 */
export default function VolumeAlerts({ days = 5 }) {
    const [alerts, setAlerts] = useState([]);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(true);
    const [open, setOpen] = useState(true);

    useEffect(() => {
        let alive = true;
        const load = () =>
            fetchVolumeAlerts({ days })
                .then((r) => alive && setAlerts(r.alerts || []))
                .catch((e) => alive && setError(e.message))
                .finally(() => alive && setLoading(false));

        setLoading(true);
        load();
        // Spikes are detected during the session, so this list changes while
        // the market is open. Matches the worker's intraday interval.
        const id = setInterval(load, 60_000);
        return () => {
            alive = false;
            clearInterval(id);
        };
    }, [days]);

    if (error) return null; // The filing alerts above are the primary view.

    return (
        <section className="overflow-hidden rounded-2xl border border-brand-border bg-brand-dark/60">
            <button
                onClick={() => setOpen((v) => !v)}
                className="flex w-full items-center justify-between px-5 py-3 text-left"
            >
                <div className="flex items-baseline gap-2">
                    <span className="text-sm font-medium text-brand-light">
                        Volume spikes
                    </span>
                    <span className="text-xs text-brand-textMuted">
                        {loading
                            ? "loading…"
                            : `${alerts.length} stock${alerts.length === 1 ? "" : "s"} trading far above their own normal`}
                    </span>
                </div>
                <span
                    className={`text-brand-textMuted transition-transform ${open ? "rotate-180" : ""}`}
                >
                    ▾
                </span>
            </button>

            {open && (
                <div className="border-t border-brand-border">
                    {loading ? (
                        <div className="space-y-2 p-4">
                            {Array.from({ length: 4 }).map((_, i) => (
                                <div key={i} className="skeleton h-10 rounded" />
                            ))}
                        </div>
                    ) : alerts.length === 0 ? (
                        <p className="px-5 py-10 text-center text-sm text-brand-textMuted">
                            No stock broke from its normal volume in this window.
                        </p>
                    ) : (
                        <div className="max-h-[420px] overflow-y-auto">
                            {alerts.map((a) => {
                                const band = bandStyle(a.conviction);
                                const up = Number(a.pct_change) >= 0;
                                return (
                                    <div
                                        key={`${a.symbol}-${a.session_date}`}
                                        className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-brand-border/40 px-5 py-2.5 last:border-0 hover:bg-brand-bg2/40"
                                    >
                                        <span className="flex w-28 shrink-0 items-center gap-1.5 font-medium text-brand-light">
                                            {a.symbol}
                                            {a.is_intraday && (
                                                <span
                                                    title="Detected during today's session; confirmed after the close"
                                                    className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-band-strong"
                                                />
                                            )}
                                        </span>

                                        <span
                                            className={`w-24 shrink-0 font-mono text-sm ${band.text}`}
                                        >
                                            {Number(a.ratio).toFixed(1)}× vol
                                        </span>

                                        <span
                                            className={`w-20 shrink-0 font-mono text-sm ${
                                                up ? "text-band-strong" : "text-red-400"
                                            }`}
                                        >
                                            {up ? "+" : ""}
                                            {Number(a.pct_change).toFixed(1)}%
                                        </span>

                                        <span className="w-28 shrink-0 font-mono text-xs text-brand-slate">
                                            ₹
                                            {Number(a.turnover_cr).toLocaleString("en-IN", {
                                                maximumFractionDigits: 0
                                            })}{" "}
                                            Cr
                                        </span>

                                        <span className="min-w-0 flex-1 truncate text-xs text-brand-textMuted">
                                            vs {Number(a.baseline_median).toLocaleString("en-IN")}{" "}
                                            median over {a.baseline_sessions} sessions
                                        </span>

                                        <span className="shrink-0 font-mono text-xs text-brand-textMuted">
                                            {a.session_date}
                                        </span>
                                    </div>
                                );
                            })}
                        </div>
                    )}

                    <p className="border-t border-brand-border/60 px-5 py-3 text-xs leading-relaxed text-brand-textMuted">
                        A stock is flagged only when its volume is several times its own
                        trailing <strong className="text-brand-slate">median</strong> AND
                        the highest in the window — so a stock that has merely been busy
                        lately stays quiet, and a run is reported once rather than every
                        day. Detection runs <strong className="text-brand-slate">during
                        the session</strong> as well as after the close, so a stock
                        that spikes on Monday morning appears on Monday morning — a
                        pulsing dot marks one still being confirmed. Scored separately
                        from the three filing rules; it does not change their scores.
                    </p>
                </div>
            )}
        </section>
    );
}
