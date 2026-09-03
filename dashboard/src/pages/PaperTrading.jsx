import React, { useCallback, useEffect, useState } from "react";

import { fetchPaperOrders, getAdminToken, setAdminToken } from "../lib/api.js";

/**
 * Paper trading — orders actually sent, with virtual money.
 *
 * Distinct from the Testing agent's shadow record, which decides what it WOULD
 * buy and contacts nothing. This page shows real API calls to MegaBull's
 * simulator: a position exists afterwards, priced against the live market, and
 * it can lose money that does not exist.
 *
 * The endpoint is hardcoded to MegaBull in engine/megabull.py. No setting on
 * this page or anywhere else can point it at a real broker.
 */

const REFRESH_MS = 30_000;

const rupees = (n) =>
    n == null
        ? "—"
        : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 0 });

function Stat({ label, value, tone }) {
    return (
        <div className="rounded-xl border border-brand-border bg-brand-bg2/50 px-3 py-2">
            <div className="text-[10px] uppercase tracking-wider text-brand-textMuted">
                {label}
            </div>
            <div
                className={`mt-0.5 font-mono text-base ${
                    tone === "up"
                        ? "text-band-strong"
                        : tone === "muted"
                          ? "text-brand-textMuted"
                          : "text-brand-light"
                }`}
            >
                {value ?? "—"}
            </div>
        </div>
    );
}

function TokenPrompt({ onSave }) {
    const [value, setValue] = useState("");
    return (
        <section className="rounded-2xl border border-brand-border bg-brand-dark/40 p-5">
            <h2 className="font-display text-lg font-semibold text-brand-light">
                Admin token
            </h2>
            <p className="mt-0.5 text-xs text-brand-textMuted">
                A trading record, even a virtual one, is not public. Stored in this
                browser only.
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
                <input
                    type="password"
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    placeholder="ALERT_ADMIN_TOKEN"
                    className="min-w-[260px] flex-1 rounded-lg border border-brand-border bg-brand-dark/60 px-3 py-2 font-mono text-sm text-brand-light placeholder:text-brand-textMuted focus:border-brand-cyan/50 focus:outline-none"
                />
                <button
                    onClick={() => onSave(value.trim())}
                    className="rounded-lg border border-brand-cyan/40 px-4 py-2 text-sm text-brand-cyan hover:bg-brand-cyan/10"
                >
                    Save
                </button>
            </div>
        </section>
    );
}

export default function PaperTrading() {
    const [token, setToken] = useState(getAdminToken());
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);

    const save = (v) => {
        setAdminToken(v);
        setToken(v);
    };

    const load = useCallback(async () => {
        if (!token) return;
        try {
            setData(await fetchPaperOrders({ days: 30 }));
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

    const acct = data?.account;
    const placed = (data?.orders || []).filter((o) => o.status === "PLACED");
    const rejected = (data?.orders || []).filter((o) => o.status !== "PLACED");

    return (
        <main className="mx-auto max-w-7xl space-y-5 px-5 py-6">
            <div className="rounded-2xl border border-band-strong/25 bg-band-strong/[0.04] p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                        <h2 className="font-display text-base font-semibold text-brand-light">
                            Paper trading
                        </h2>
                        <p className="mt-0.5 text-xs leading-relaxed text-brand-textMuted">
                            Buys on an alert with virtual money, on MegaBull's simulator.
                            Real API calls and real prices; the money is not real and no
                            broker is connected. The endpoint is fixed in code — nothing
                            here can reach a live account.
                        </p>
                    </div>
                    <span className="rounded-full border border-band-strong/40 bg-band-strong/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-band-strong">
                        Virtual money
                    </span>
                </div>
            </div>

            {!token && <TokenPrompt onSave={save} />}

            {token && error && (
                <p className="rounded-2xl border border-band-watch/40 bg-band-watch/5 p-4 text-sm text-band-watch">
                    {error.includes("401")
                        ? "Admin token missing or wrong."
                        : error.includes("503")
                          ? "Disabled — ALERT_ADMIN_TOKEN is not set on the server."
                          : error}
                </p>
            )}

            {token && !error && (
                <>
                    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                        <Stat label="Virtual money" value={rupees(acct?.virtual_money)} />
                        <Stat label="Available" value={rupees(acct?.available)} />
                        <Stat label="Deployed" value={rupees(data?.deployed_inr)} />
                        <Stat
                            label="Positions taken"
                            value={data ? `${data.placed} of ${data.count}` : "—"}
                        />
                    </div>

                    {acct && !acct.live && (
                        <p className="rounded-xl border border-brand-border bg-brand-dark/40 px-4 py-3 text-sm text-brand-textMuted">
                            Not trading: {acct.reason}
                        </p>
                    )}

                    {data && !data.gate?.enabled && (
                        <p className="rounded-xl border border-brand-border bg-brand-dark/40 px-4 py-3 text-xs text-brand-textMuted">
                            Recording only — set{" "}
                            <code className="text-brand-slate">
                                ALERT_PAPER_TRADING_ENABLED=true
                            </code>{" "}
                            to let it place orders. Gate: STRONG, order ≥ ₹
                            {data.gate?.min_order_cr} Cr, ≥ {data.gate?.min_order_to_mcap_pct}%
                            of market cap, {rupees(data.gate?.per_position_inr)} per position,
                            max {data.gate?.max_per_day}/day.
                        </p>
                    )}

                    <section className="rounded-2xl border border-brand-border bg-brand-dark/40 p-5">
                        <h2 className="mb-4 font-display text-lg font-semibold text-brand-light">
                            Positions bought
                        </h2>
                        {placed.length === 0 ? (
                            <p className="rounded-xl border border-dashed border-brand-border py-8 text-center text-sm text-brand-textMuted">
                                Nothing bought yet. The gate admits roughly two a week.
                            </p>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-sm">
                                    <thead className="text-[10px] uppercase tracking-wider text-brand-textMuted">
                                        <tr>
                                            {["Symbol", "Qty", "Price", "Value", "% of co.", "When"].map(
                                                (h) => (
                                                    <th key={h} className="pb-2 pr-4 font-medium">
                                                        {h}
                                                    </th>
                                                )
                                            )}
                                        </tr>
                                    </thead>
                                    <tbody className="font-mono text-brand-slate">
                                        {placed.map((o) => (
                                            <tr
                                                key={o.id}
                                                className="border-t border-brand-border/60"
                                            >
                                                <td className="py-2 pr-4 text-brand-light">
                                                    {o.company_symbol}
                                                </td>
                                                <td className="py-2 pr-4">{o.quantity}</td>
                                                <td className="py-2 pr-4">{o.price ?? "—"}</td>
                                                <td className="py-2 pr-4">
                                                    {rupees((o.quantity || 0) * (o.price || 0))}
                                                </td>
                                                <td className="py-2 pr-4 text-brand-cyan">
                                                    {o.order_to_mcap_pct ?? "—"}%
                                                </td>
                                                <td className="py-2 pr-4 text-xs text-brand-textMuted">
                                                    {(o.placed_at || "").slice(0, 16).replace("T", " ")}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}

                        {rejected.length > 0 && (
                            <details className="mt-4">
                                <summary className="cursor-pointer text-xs text-brand-textMuted hover:text-brand-slate">
                                    {rejected.length} alert
                                    {rejected.length === 1 ? "" : "s"} not bought — and why
                                </summary>
                                <ul className="mt-2 space-y-1">
                                    {rejected.slice(0, 40).map((o) => (
                                        <li key={o.id} className="text-xs text-brand-textMuted">
                                            <span className="font-mono text-brand-slate">
                                                {o.company_symbol}
                                            </span>{" "}
                                            {o.status === "FAILED" && (
                                                <span className="text-band-watch">rejected · </span>
                                            )}
                                            {o.reason}
                                        </li>
                                    ))}
                                </ul>
                            </details>
                        )}
                    </section>
                </>
            )}
        </main>
    );
}
