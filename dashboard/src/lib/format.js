// Display helpers. Indian numbering throughout — figures are quoted in crore
// and read by people who expect 1,23,456 rather than 123,456.

export function formatCr(value) {
    if (value === null || value === undefined) return "—";
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    if (n >= 1000) return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })} Cr`;
    return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })} Cr`;
}

export function formatPct(value) {
    if (value === null || value === undefined) return "—";
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return `${n > 0 ? "+" : ""}${n.toLocaleString("en-IN", { maximumFractionDigits: 1 })}%`;
}

export function timeAgo(iso) {
    if (!iso) return "—";
    const then = new Date(iso);
    if (Number.isNaN(then.getTime())) return "—";

    const mins = Math.round((Date.now() - then.getTime()) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;

    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;

    const days = Math.round(hrs / 24);
    return days === 1 ? "yesterday" : `${days}d ago`;
}

export function formatDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString("en-IN", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        hour12: true
    });
}

// Conviction -> the two classes every badge/bar needs. Kept as full class
// strings because Tailwind's scanner cannot see interpolated names.
export const BAND_STYLES = {
    STRONG: {
        text: "text-band-strong",
        bg: "bg-band-strong/10",
        border: "border-band-strong/40",
        bar: "bg-band-strong",
        label: "Strong"
    },
    MODERATE: {
        text: "text-band-moderate",
        bg: "bg-band-moderate/10",
        border: "border-band-moderate/40",
        bar: "bg-band-moderate",
        label: "Moderate"
    },
    WATCH: {
        text: "text-band-watch",
        bg: "bg-band-watch/10",
        border: "border-band-watch/40",
        bar: "bg-band-watch",
        label: "Watch"
    }
};

export const bandStyle = (conviction) =>
    BAND_STYLES[conviction] || BAND_STYLES.WATCH;

export const RULE_LABELS = {
    PROFIT_GROWTH: "Profit growth",
    REVENUE_GROWTH: "Revenue growth",
    ORDER_WIN: "Order win"
};


// The clock time an alert reached the dashboard, in IST. `created_at` is
// tz-aware from Postgres, so the browser renders it in the viewer's zone —
// pinned to IST here so it lines up with the exchange timestamp beside it.
export function shortTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleTimeString("en-IN", {
        hour: "2-digit", minute: "2-digit", hour12: true,
        timeZone: "Asia/Kolkata"
    });
}

// How long the alert took to arrive. announced_at is naive IST from the
// exchange; created_at is tz-aware. Parsing the first as UTC and correcting by
// 5h30m is what keeps the subtraction honest across a viewer's timezone.
export function alertDelay(alert) {
    if (!alert?.announced_at || !alert?.created_at) return "";
    const announced = new Date(alert.announced_at + "Z").getTime() - 5.5 * 3600 * 1000;
    const seen = new Date(alert.created_at).getTime();
    const secs = Math.round((seen - announced) / 1000);
    if (!Number.isFinite(secs) || secs < 0 || secs > 86400 * 7) return "";
    if (secs < 90) return `${secs}s after the exchange`;
    const mins = Math.round(secs / 60);
    if (mins < 90) return `${mins} min after the exchange`;
    return `${Math.round(mins / 60)} h after the exchange`;
}
