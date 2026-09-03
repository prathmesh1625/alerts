import React, { useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes } from "react-router-dom";

import Alerts from "./pages/Alerts.jsx";
import Filings from "./pages/Filings.jsx";
import Market from "./pages/Market.jsx";
import TestingAgent from "./pages/TestingAgent.jsx";

/**
 * The shell: chrome, navigation, and the two views.
 *
 *   /          alerts    — what cleared the formula, ranked by conviction
 *   /filings   research  — everything that came in, and what the agent made of
 *                          it, with the source PDF one click away
 *   /market    live      — NSE prices, volume and turnover, refreshed while you
 *                          watch, with alerted companies flagged
 *   /testing   agent     — the unproven parts: what the trading gate would have
 *                          bought, the broker session, near misses, timings.
 *                          Deliberately separate, so experimenting there cannot
 *                          disturb the alerts view, which is the thing that
 *                          works and is watched daily.
 */

function Nav() {
    const link = ({ isActive }) =>
        `rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
            isActive
                ? "bg-brand-cyan/15 text-brand-cyan"
                : "text-brand-textMuted hover:text-brand-slate"
        }`;

    return (
        <nav className="flex gap-1 rounded-xl border border-brand-border bg-brand-dark/60 p-1">
            <NavLink to="/" end className={link}>
                Alerts
            </NavLink>
            <NavLink to="/filings" className={link}>
                All filings
            </NavLink>
            <NavLink to="/market" className={link}>
                Market
            </NavLink>
            <NavLink to="/testing" className={link}>
                Testing agent
            </NavLink>
        </nav>
    );
}

function Header({ lastUpdated }) {
    return (
        <header className="border-b border-brand-border/70">
            <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-5 py-5">
                <div>
                    <h1 className="font-display text-2xl font-semibold tracking-tight">
                        <span className="bg-brand-grad bg-clip-text text-transparent">
                            Momentum Alerts
                        </span>
                    </h1>
                    <p className="mt-0.5 text-sm text-brand-slate">
                        NSE &amp; BSE filings, read and scored automatically
                    </p>
                </div>

                <div className="flex items-center gap-4">
                    {lastUpdated && (
                        <span className="hidden font-mono text-xs text-brand-textMuted sm:block">
                            {lastUpdated.toLocaleTimeString("en-IN", {
                                hour: "2-digit",
                                minute: "2-digit",
                                hour12: true
                            })}
                        </span>
                    )}
                    <Nav />
                </div>
            </div>
        </header>
    );
}

export default function App() {
    const [lastUpdated, setLastUpdated] = useState(null);

    return (
        <BrowserRouter>
            <div className="min-h-screen">
                <div className="grid-bg fixed inset-0 -z-10 opacity-[0.35]" />

                <Header lastUpdated={lastUpdated} />

                <Routes>
                    <Route path="/" element={<Alerts onUpdated={setLastUpdated} />} />
                    <Route path="/filings" element={<Filings />} />
                    <Route path="/market" element={<Market />} />
                    <Route path="/testing" element={<TestingAgent />} />
                    <Route path="*" element={<Navigate to="/" replace />} />
                </Routes>

                <footer className="mx-auto max-w-7xl px-5 pb-10 pt-4">
                    <p className="border-t border-brand-border/70 pt-4 text-xs leading-relaxed text-brand-textMuted">
                        Alerts are generated automatically by reading exchange filings
                        with an AI model and applying a fixed screening formula. Figures
                        are extracted from PDFs and may contain errors — check the linked
                        filing before acting on any alert. This is a screening tool, not
                        investment advice, and a filing clearing the formula is not a
                        prediction that the stock will rise.
                    </p>
                </footer>
            </div>
        </BrowserRouter>
    );
}
