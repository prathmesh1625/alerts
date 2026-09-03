// Thin fetch wrapper around the engine's read-only API (engine/api.py).
// No axios: three GETs don't justify the dependency.

const BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");

// The admin token for the gated endpoints — account funds, the broker session,
// and the trading record. Kept in this browser only: it is never sent to the
// alert endpoints, which are public and do not want it.
const TOKEN_KEY = "alerts.adminToken";

export const getAdminToken = () => {
    try {
        return localStorage.getItem(TOKEN_KEY) || "";
    } catch {
        return "";        // private window, or storage blocked
    }
};

export const setAdminToken = (value) => {
    try {
        if (value) localStorage.setItem(TOKEN_KEY, value);
        else localStorage.removeItem(TOKEN_KEY);
    } catch {
        /* nothing we can do, and nothing that should break the page */
    }
};

async function get(path, params = {}, { admin = false } = {}) {
    const url = new URL(`${BASE}${path}`, window.location.origin);
    Object.entries(params).forEach(([k, v]) => {
        if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
    });

    const headers = { Accept: "application/json" };
    if (admin) {
        const t = getAdminToken();
        if (t) headers["X-Admin-Token"] = t;
    }

    const res = await fetch(url, { headers });
    if (!res.ok) {
        let detail = res.statusText;
        try {
            detail = (await res.json()).detail || detail;
        } catch {
            /* non-JSON error body — the status text is all we have */
        }
        throw new Error(`${res.status} ${detail}`);
    }
    return res.json();
}

export const fetchAlerts = (params) => get("/api/alerts", params);
export const fetchCompanies = (params) => get("/api/companies", params);
export const fetchFilings = (params) => get("/api/filings", params);
export const fetchMarket = (params) => get("/api/market", params);
export const fetchVolumeAlerts = (params) => get("/api/volume-alerts", params);
export const fetchStats = (params) => get("/api/stats", params);
export const fetchConfig = () => get("/api/config");

// --- the testing agent: gated, and deliberately separate ---------------------
//
// These sit behind the admin token because they touch the broker session and
// the record of what the account would have bought. They are NOT part of the
// alerts view, which stays public and unchanged.
export const fetchPaperTrades = (params) =>
    get("/api/paper-trades", params, { admin: true });
export const fetchNearMisses = (params) => get("/api/near-misses", params);
export const fetchLatency = (params) => get("/api/latency", params);

// Works for ANY filing, not just those that raised an alert — the API falls
// back to downloading from the exchange when nothing is cached locally.
export const pdfUrl = (announcementId) =>
    `${BASE}/api/filings/${announcementId}/pdf`;
