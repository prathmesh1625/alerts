import React, { useState } from "react";

import {
    bandStyle,
    formatCr,
    formatDateTime,
    formatPct,
    timeAgo
} from "../lib/format.js";
import { pdfUrl } from "../lib/api.js";

/** The score bar — the visual ranking cue, so it reads before any text does. */
function ScoreBar({ score, band }) {
    return (
        <div className="flex items-center gap-3">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-brand-grid">
                <div
                    className={`h-full rounded-full ${band.bar} transition-[width] duration-500`}
                    style={{ width: `${Math.min(100, Math.max(0, score))}%` }}
                />
            </div>
            <span className={`font-mono text-sm font-medium ${band.text}`}>
                {Number(score).toFixed(0)}
            </span>
        </div>
    );
}

/** One rule's contribution — this is what makes an alert auditable. */
function RuleRow({ rule }) {
    const hit = rule.hit;
    return (
        <div
            className={`flex items-baseline justify-between gap-3 rounded-lg px-3 py-2 text-sm ${
                hit ? "bg-brand-cyan/[0.07]" : "opacity-45"
            }`}
        >
            <div className="flex min-w-0 items-baseline gap-2">
                <span className={hit ? "text-brand-cyan" : "text-brand-textMuted"}>
                    {hit ? "✓" : "○"}
                </span>
                <span className="truncate text-brand-slate">{rule.label}</span>
            </div>
            <div className="flex shrink-0 items-baseline gap-3">
                <span className={`font-mono ${hit ? "text-brand-light" : "text-brand-textMuted"}`}>
                    {rule.display}
                </span>
                <span className="w-14 text-right font-mono text-xs text-brand-textMuted">
                    {hit ? `+${rule.points.toFixed(1)}` : "—"}
                </span>
            </div>
        </div>
    );
}

export default function AlertCard({ alert }) {
    const [open, setOpen] = useState(false);
    const band = bandStyle(alert.conviction);
    const rules = alert.breakdown?.rules || [];
    const notes = rules.filter((r) => r.hit && r.note).map((r) => r.note);

    return (
        <article
            className={`animate-fade-in-up overflow-hidden rounded-2xl border bg-brand-dark/80 shadow-card backdrop-blur transition-colors hover:border-brand-cyan/40 ${band.border}`}
        >
            <div className="p-5">
                {/* ── header ─────────────────────────────────────────────── */}
                <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                        <div className="flex items-center gap-2">
                            <h3 className="font-display text-xl font-semibold tracking-tight text-brand-light">
                                {alert.company_symbol}
                            </h3>
                            <span
                                className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${band.bg} ${band.text} ${band.border}`}
                            >
                                {band.label}
                            </span>
                        </div>
                        {alert.company_name &&
                            alert.company_name !== alert.company_symbol && (
                                <p className="mt-0.5 truncate text-sm text-brand-slate">
                                    {alert.company_name}
                                </p>
                            )}
                    </div>

                    <div className="shrink-0 text-right">
                        <div className="font-mono text-xs text-brand-textMuted">
                            {timeAgo(alert.announced_at)}
                        </div>
                        <div className="mt-1 flex items-center justify-end gap-1.5 text-[10px] uppercase tracking-wider text-brand-textMuted">
                            {alert.exchange && (
                                <span className="rounded border border-brand-border px-1 py-px">
                                    {alert.exchange}
                                </span>
                            )}
                            {alert.market_cap_cr != null && (
                                <span title="Market capitalisation">
                                    ₹
                                    {Number(alert.market_cap_cr).toLocaleString("en-IN", {
                                        maximumFractionDigits: 0
                                    })}{" "}
                                    Cr
                                </span>
                            )}
                            {alert.document_type && (
                                <span>{alert.document_type.replace("_", " ")}</span>
                            )}
                        </div>
                    </div>
                </div>

                {/* ── the call ───────────────────────────────────────────── */}
                <p className="mt-3 text-[15px] font-medium leading-snug text-brand-light">
                    {alert.headline}
                </p>

                {notes.length > 0 && (
                    <ul className="mt-1.5 space-y-0.5">
                        {notes.map((n, i) => (
                            <li key={i} className="text-xs text-brand-slate">
                                · {n}
                            </li>
                        ))}
                    </ul>
                )}

                {/* ── the three headline numbers ─────────────────────────── */}
                <div className="mt-4 grid grid-cols-3 gap-2">
                    {[
                        ["Profit YoY", formatPct(alert.profit_growth_pct), "PROFIT_GROWTH"],
                        ["Revenue YoY", formatPct(alert.revenue_growth_pct), "REVENUE_GROWTH"],
                        ["Orders", formatCr(alert.order_value_cr), "ORDER_WIN"]
                    ].map(([label, value, key]) => {
                        const hit = (alert.rules_hit || []).includes(key);
                        return (
                            <div
                                key={key}
                                className={`rounded-lg border px-2.5 py-2 ${
                                    hit
                                        ? "border-brand-cyan/30 bg-brand-cyan/[0.06]"
                                        : "border-brand-border bg-brand-bg2/50"
                                }`}
                            >
                                <div className="text-[10px] uppercase tracking-wider text-brand-textMuted">
                                    {label}
                                </div>
                                <div
                                    className={`mt-0.5 font-mono text-sm ${
                                        hit ? "text-brand-cyan" : "text-brand-slate"
                                    }`}
                                >
                                    {value}
                                </div>
                            </div>
                        );
                    })}
                </div>

                <div className="mt-4">
                    <ScoreBar score={Number(alert.score)} band={band} />
                </div>
            </div>

            {/* ── evidence drawer ────────────────────────────────────────── */}
            <div className="border-t border-brand-border/70">
                <button
                    onClick={() => setOpen((v) => !v)}
                    className="flex w-full items-center justify-between px-5 py-2.5 text-xs text-brand-textMuted transition-colors hover:text-brand-cyan"
                >
                    <span>{open ? "Hide" : "Why this fired"}</span>
                    <span className={`transition-transform ${open ? "rotate-180" : ""}`}>▾</span>
                </button>

                {open && (
                    <div className="space-y-4 border-t border-brand-border/70 bg-brand-bg2/60 px-5 py-4">
                        <div className="space-y-1">
                            {rules.map((r) => (
                                <RuleRow key={r.rule} rule={r} />
                            ))}
                            <div className="flex justify-between px-3 pt-2 text-xs text-brand-textMuted">
                                <span>Total</span>
                                <span className="font-mono">
                                    {Number(alert.score).toFixed(2)} /{" "}
                                    {alert.breakdown?.max_possible ?? 100}
                                </span>
                            </div>
                        </div>

                        {alert.evidence?.length > 0 && (
                            <div>
                                <div className="mb-1.5 text-[10px] uppercase tracking-wider text-brand-textMuted">
                                    From the filing
                                </div>
                                <div className="space-y-1.5">
                                    {alert.evidence.map((q, i) => (
                                        <blockquote
                                            key={i}
                                            className="border-l-2 border-brand-cyan/40 pl-3 text-xs italic leading-relaxed text-brand-slate"
                                        >
                                            “{q}”
                                        </blockquote>
                                    ))}
                                </div>
                            </div>
                        )}

                        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
                            {[
                                ["Filing", alert.title],
                                ["Period", alert.reporting_period],
                                ["Basis", alert.basis],
                                ["Announced", formatDateTime(alert.announced_at)]
                            ]
                                .filter(([, v]) => v)
                                .map(([k, v]) => (
                                    <div key={k} className="col-span-2 flex gap-2">
                                        <dt className="shrink-0 text-brand-textMuted">{k}:</dt>
                                        <dd className="min-w-0 text-brand-slate">{v}</dd>
                                    </div>
                                ))}
                        </dl>

                        <div className="flex gap-2 pt-1">
                            <a
                                href={pdfUrl(alert.announcement_id)}
                                target="_blank"
                                rel="noreferrer"
                                className="rounded-lg border border-brand-border px-3 py-1.5 text-xs text-brand-slate transition-colors hover:border-brand-cyan/50 hover:text-brand-cyan"
                            >
                                Open PDF
                            </a>
                            {alert.pdf_url && (
                                <a
                                    href={alert.pdf_url}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="rounded-lg border border-brand-border px-3 py-1.5 text-xs text-brand-slate transition-colors hover:border-brand-cyan/50 hover:text-brand-cyan"
                                >
                                    Source on exchange
                                </a>
                            )}
                        </div>
                    </div>
                )}
            </div>
        </article>
    );
}
