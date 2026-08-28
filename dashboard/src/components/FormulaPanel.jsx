import React from "react";

/**
 * The formula, rendered from the engine's own /api/config.
 *
 * Reading it from the API rather than hard-coding it means retuning a threshold
 * in engine/config.py can never leave this panel describing rules that are no
 * longer in force — which would be worse than showing nothing.
 */
export default function FormulaPanel({ config, open, onToggle }) {
    if (!config) return null;

    return (
        <div className="rounded-2xl border border-brand-border bg-brand-dark/60">
            <button
                onClick={onToggle}
                className="flex w-full items-center justify-between px-5 py-3 text-left"
            >
                <div>
                    <span className="text-sm font-medium text-brand-light">
                        Alert formula
                    </span>
                    <span className="ml-2 text-xs text-brand-textMuted">
                        {config.rules.length}{" "}
                        {config.rules.length === 1 ? "rule" : "rules"} · alert at
                        score ≥ {config.alert_min_score} · {config.model}
                    </span>
                </div>
                <span
                    className={`text-brand-textMuted transition-transform ${
                        open ? "rotate-180" : ""
                    }`}
                >
                    ▾
                </span>
            </button>

            {open && (
                <div className="border-t border-brand-border px-5 py-4">
                    <div className="space-y-2">
                        {config.rules.map((r, i) => (
                            <div
                                key={r.key}
                                className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-lg bg-brand-bg2/60 px-3 py-2"
                            >
                                <span className="font-mono text-xs text-brand-textMuted">
                                    {i + 1}.
                                </span>
                                <span className="text-sm text-brand-light">{r.label}</span>
                                <span className="font-mono text-sm text-brand-cyan">
                                    ≥ {r.threshold}
                                    {r.unit.startsWith("%") ? "" : " "}
                                    {r.unit}
                                </span>
                                <span className="ml-auto font-mono text-xs text-brand-textMuted">
                                    up to {r.weight} pts · full at {r.full_at} {r.unit}
                                </span>
                            </div>
                        ))}
                    </div>

                    {config.disabled_rules?.length > 0 && (
                        <p className="mt-3 text-xs text-brand-textMuted">
                            Currently off:{" "}
                            <span className="text-brand-slate">
                                {config.disabled_rules.join(", ")}
                            </span>{" "}
                            — being held back until the order-win rule is precise
                            enough to trust on its own.
                        </p>
                    )}
                    <p className="mt-3 text-xs leading-relaxed text-brand-textMuted">
                        Each rule is scored independently. Clearing a rule&apos;s
                        threshold banks {Math.round(config.base_credit * 100)}% of its
                        weight; the remainder is earned by how far past the threshold
                        the filing lands, so the ranking reflects the size of the beat
                        rather than just the fact of it. Growth is measured
                        year-over-year against the corresponding quarter of the
                        previous year. Bands: Strong ≥ {config.bands.strong},
                        Moderate ≥ {config.bands.moderate}, Watch ≥{" "}
                        {config.alert_min_score}.
                        {config.min_market_cap_cr > 0 && (
                            <>
                                {" "}Companies below ₹
                                {Number(config.min_market_cap_cr).toLocaleString("en-IN")}{" "}
                                Cr market cap are skipped — a filing says nothing
                                about the size of the business behind it.
                            </>
                        )}
                    </p>
                </div>
            )}
        </div>
    );
}
