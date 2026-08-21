// Thin fetch wrapper around the engine's read-only API (engine/api.py).
// No axios: three GETs don't justify the dependency.

const BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");

async function get(path, params = {}) {
    const url = new URL(`${BASE}${path}`, window.location.origin);
    Object.entries(params).forEach(([k, v]) => {
        if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
    });

    const res = await fetch(url, { headers: { Accept: "application/json" } });
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

// Works for ANY filing, not just those that raised an alert — the API falls
// back to downloading from the exchange when nothing is cached locally.
export const pdfUrl = (announcementId) =>
    `${BASE}/api/filings/${announcementId}/pdf`;
