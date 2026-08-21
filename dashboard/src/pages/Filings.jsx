import React, { useCallback, useEffect, useMemo, useState } from "react";

import { fetchCompanies, fetchFilings, pdfUrl } from "../lib/api.js";
import { formatDateTime, timeAgo } from "../lib/format.js";

/**
 * The research view.
 *
 * The alerts page answers "what should I look at?". This one answers "what came
 * in, and what did the agent make of it?" — every filing, including the ones it
 * skipped and the reason why. That is the half you need to audit the screen
 * rather than trust it, and to pull PDFs for your own reading.
 */

const WINDOWS = [
    { label: "Today", value: 1 },
    { label: "1 week", value: 7 },
    { label: "1 month", value: 30 },
    { label: "3 months", value: 90 },
    { label: "1 year", value: 365 }
];

const STATUSES = [
    { label: "All", value: "" },
    { label: "Alerts", value: "ALERT" },
    { label: "Read by AI", value: "ANALYZED" },
    { label: "Skipped", value: "SKIPPED" }
];

const STATUS_STYLE = {
    ALERT: "text-band-strong border-band-strong/40 bg-band-strong/10",
    ANALYZED: "text-band-moderate border-band-moderate/40 bg-band-moderate/10",
    SKIPPED: "text-brand-textMuted border-brand-border bg-brand-bg2/60",
    FAILED: "text-band-watch border-band-watch/40 bg-band-watch/10",
    PENDING: "text-brand-slate border-brand-border bg-brand-bg2/60"
};

function Segmented({ options, value, onChange }) {
    return (
        <div className="flex flex-wrap rounded-lg border border-brand-border bg-brand-dark/60 p-0.5">
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

/** One filing row — the verdict, and a way to read the source. */
function FilingRow({ filing }) {
    const [open, setOpen] = useState(false);
    const status = filing.is_alert ? "ALERT" : filing.status;
    const signals = filing.raw_signals;

    return (
        <div className="border-b border-brand-border/60 last:border-0">
            <div className="flex items-start gap-3 px-4 py-3 transition-colors hover:bg-brand-bg2/40">
                <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                        <span className="font-display text-sm font-semibold text-brand-light">
                            {filing.company_symbol}
                        </span>
                        <span
                            className={`rounded border px-1.5 py-px text-[10px] font-medium uppercase tracking-wider ${
                                STATUS_STYLE[status] || STATUS_STYLE.PENDING
                            }`}
                        >
                            {status === "ANALYZED" ? "read" : status.toLowerCase()}
                        </span>
                        <span className="rounded border border-brand-border px-1.5 py-px text-[10px] text-brand-textMuted">
                            {filing.exchange}
                        </span>
                        {filing.score != null && filing.score > 0 && (
                            <span className="font-mono text-[11px] text-brand-cyan">
                                {Number(filing.score).toFixed(0)} pts
                            </span>
                        )}
                    </div>

                    <p className="mt-1 truncate text-sm text-brand-slate" title={filing.title || ""}>
                        {filing.title || <span className="italic opacity-60">no title</span>}
                    </p>

                    {filing.headline && (
                        <p className="mt-0.5 text-xs text-brand-cyan">{filing.headline}</p>
                    )}
                    {!filing.is_alert && filing.skip_reason && (
                        <p className="mt-0.5 text-xs text-brand-textMuted">
                            {filing.skip_reason}
                        </p>
                    )}
                    {filing.error && (
                        <p className="mt-0.5 text-xs text-band-watch">{filing.error}</p>
                    )}
                </div>

                <div className="flex shrink-0 flex-col items-end gap-1.5">
                    <span className="whitespace-nowrap font-mono text-[11px] text-brand-textMuted">
                        {timeAgo(filing.announced_at)}
                    </span>
                    <div className="flex gap-1.5">
                        <a
                            href={pdfUrl(filing.announcement_id)}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded-md border border-brand-border px-2 py-1 text-[11px] text-brand-slate transition-colors hover:border-brand-cyan/50 hover:text-brand-cyan"
                            title="Open the filing PDF"
                        >
                            PDF
                        </a>
                        {signals && (
                            <button
                                onClick={() => setOpen((v) => !v)}
                                className="rounded-md border border-brand-border px-2 py-1 text-[11px] text-brand-slate transition-colors hover:border-brand-cyan/50 hover:text-brand-cyan"
                                title="What the model read out of it"
                            >
                                {open ? "hide" : "data"}
                            </button>
                        )}
                    </div>
                </div>
            </div>

            {open && signals && (
                <div className="border-t border-brand-border/60 bg-brand-bg2/60 px-4 py-3">
                    <div className="mb-2 flex items-center gap-3 text-[11px] text-brand-textMuted">
                        <span>{formatDateTime(filing.announced_at)}</span>
                        {filing.document_type && <span>· {filing.document_type}</span>}
                        {filing.pdf_url && (
                            <a
                                href={filing.pdf_url}
                                target="_blank"
                                rel="noreferrer"
                                className="hover:text-brand-cyan"
                            >
                                · source on exchange
                            </a>
                        )}
                    </div>
                    <pre className="max-h-72 overflow-auto rounded-lg bg-brand-navy/70 p-3 text-[11px] leading-relaxed text-brand-slate">
                        {JSON.stringify(signals, null, 2)}
                    </pre>
                </div>
            )}
        </div>
    );
}

export default function Filings() {
    const [companies, setCompanies] = useState([]);
    const [filings, setFilings] = useState([]);
    const [symbol, setSymbol] = useState(null);
    const [days, setDays] = useState(30);
    const [status, setStatus] = useState("");
    const [companyQuery, setCompanyQuery] = useState("");
    const [titleQuery, setTitleQuery] = useState("");
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    useEffect(() => {
        fetchCompanies({ days })
            .then((r) => setCompanies(r.companies || []))
            .catch((e) => setError(e.message));
    }, [days]);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const r = await fetchFilings({
                days,
                symbol: symbol || undefined,
                status: status || undefined,
                q: titleQuery || undefined,
                limit: 300
            });
            setFilings(r.filings || []);
            setError(null);
        } catch (e) {
            setError(e.message);
        } finally {
            setLoading(false);
        }
    }, [days, symbol, status, titleQuery]);

    useEffect(() => {
        const t = setTimeout(load, titleQuery ? 350 : 0); // debounce typing
        return () => clearTimeout(t);
    }, [load, titleQuery]);

    // Company filtering is client-side: the list is already capped server-side
    // and a round-trip per keystroke would buy nothing.
    const visibleCompanies = useMemo(() => {
        const q = companyQuery.trim().toLowerCase();
        if (!q) return companies;
        return companies.filter(
            (c) =>
                c.company_symbol?.toLowerCase().includes(q) ||
                c.company_name?.toLowerCase().includes(q)
        );
    }, [companies, companyQuery]);

    return (
        <div className="mx-auto max-w-7xl px-5 py-6">
            <div className="mb-4 flex flex-wrap items-center gap-3">
                <Segmented options={WINDOWS} value={days} onChange={setDays} />
                <Segmented options={STATUSES} value={status} onChange={setStatus} />
                <input
                    value={titleQuery}
                    onChange={(e) => setTitleQuery(e.target.value)}
                    placeholder="Search filing titles…"
                    className="min-w-[180px] flex-1 rounded-lg border border-brand-border bg-brand-dark/60 px-3 py-2 text-sm text-brand-light placeholder:text-brand-textMuted focus:border-brand-cyan/50 focus:outline-none"
                />
            </div>

            {error && (
                <div className="mb-4 rounded-lg border border-band-watch/40 bg-band-watch/5 px-4 py-3 text-sm text-band-watch">
                    {error}
                </div>
            )}

            <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
                {/* ── companies ───────────────────────────────────────────── */}
                <aside className="rounded-2xl border border-brand-border bg-brand-dark/60">
                    <div className="border-b border-brand-border p-3">
                        <input
                            value={companyQuery}
                            onChange={(e) => setCompanyQuery(e.target.value)}
                            placeholder="Filter companies…"
                            className="w-full rounded-lg border border-brand-border bg-brand-bg2/60 px-3 py-2 text-sm text-brand-light placeholder:text-brand-textMuted focus:border-brand-cyan/50 focus:outline-none"
                        />
                    </div>

                    <div className="max-h-[70vh] overflow-y-auto">
                        <button
                            onClick={() => setSymbol(null)}
                            className={`flex w-full items-center justify-between px-4 py-2.5 text-left text-sm transition-colors ${
                                !symbol
                                    ? "bg-brand-cyan/10 text-brand-cyan"
                                    : "text-brand-slate hover:bg-brand-bg2/50"
                            }`}
                        >
                            <span>All companies</span>
                            <span className="font-mono text-[11px] text-brand-textMuted">
                                {companies.length}
                            </span>
                        </button>

                        {visibleCompanies.map((c) => (
                            <button
                                key={c.company_symbol}
                                onClick={() => setSymbol(c.company_symbol)}
                                className={`w-full border-t border-brand-border/40 px-4 py-2.5 text-left transition-colors ${
                                    symbol === c.company_symbol
                                        ? "bg-brand-cyan/10"
                                        : "hover:bg-brand-bg2/50"
                                }`}
                            >
                                <div className="flex items-center justify-between gap-2">
                                    <span
                                        className={`truncate text-sm font-medium ${
                                            symbol === c.company_symbol
                                                ? "text-brand-cyan"
                                                : "text-brand-light"
                                        }`}
                                    >
                                        {c.company_symbol}
                                    </span>
                                    {c.alerts > 0 && (
                                        <span className="shrink-0 rounded bg-band-strong/15 px-1.5 py-px text-[10px] font-medium text-band-strong">
                                            {c.alerts}
                                        </span>
                                    )}
                                </div>
                                {c.company_name && (
                                    <div className="truncate text-[11px] text-brand-textMuted">
                                        {c.company_name}
                                    </div>
                                )}
                                <div className="mt-0.5 font-mono text-[10px] text-brand-textMuted">
                                    {c.filings} filing{c.filings === 1 ? "" : "s"} ·{" "}
                                    {c.analyzed} read
                                </div>
                            </button>
                        ))}

                        {visibleCompanies.length === 0 && (
                            <p className="px-4 py-6 text-center text-sm text-brand-textMuted">
                                No company matches.
                            </p>
                        )}
                    </div>
                </aside>

                {/* ── filings ─────────────────────────────────────────────── */}
                <section className="overflow-hidden rounded-2xl border border-brand-border bg-brand-dark/60">
                    <div className="flex items-center justify-between border-b border-brand-border px-4 py-2.5">
                        <h2 className="text-sm font-medium text-brand-light">
                            {symbol || "All companies"}
                        </h2>
                        <span className="font-mono text-xs text-brand-textMuted">
                            {loading ? "loading…" : `${filings.length} filing${filings.length === 1 ? "" : "s"}`}
                        </span>
                    </div>

                    {loading ? (
                        <div className="space-y-2 p-4">
                            {Array.from({ length: 8 }).map((_, i) => (
                                <div key={i} className="skeleton h-14 rounded-lg" />
                            ))}
                        </div>
                    ) : filings.length === 0 ? (
                        <p className="px-4 py-16 text-center text-sm text-brand-textMuted">
                            No filings match these filters.
                        </p>
                    ) : (
                        <div className="max-h-[70vh] overflow-y-auto">
                            {filings.map((f) => (
                                <FilingRow key={f.announcement_id} filing={f} />
                            ))}
                        </div>
                    )}
                </section>
            </div>

            <p className="mt-4 text-xs leading-relaxed text-brand-textMuted">
                Every filing the scraper collected, whether or not it cleared the
                formula. &ldquo;Skipped&rdquo; means the agent decided it could not
                contain a results statement or an order win — the reason is shown, and
                the PDF is one click away if you want to check that judgement
                yourself. PDFs are fetched from the exchange on demand, so the first
                open of an older filing may take a moment.
            </p>
        </div>
    );
}
