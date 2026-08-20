/** @type {import('tailwindcss').Config} */
// Palette lifted verbatim from shares/frontend/tailwind.config.js so this
// dashboard reads as part of the same product, not a bolted-on tool.
export default {
    content: [
        "./index.html",
        "./src/**/*.{js,ts,jsx,tsx}"
    ],
    theme: {
        extend: {
            colors: {
                brand: {
                    navy: "#07090F",
                    bg: "#07090F",
                    bg2: "#0B0E16",
                    dark: "#10141E",
                    dark2: "#141927",
                    cyan: "#33D097",
                    teal: "#2DD4BF",
                    sky: "#38BDF8",
                    slate: "#98A0AE",
                    light: "#EDF0F4",
                    border: "#1E2535",
                    grid: "#1A2230",
                    accentSecondary: "#238F6A",
                    textMuted: "#646E7E"
                },
                // Conviction bands. Amber/slate rather than red for the lower
                // bands: every alert here is a positive signal, so a "weak"
                // one must not be coloured like a warning.
                band: {
                    strong: "#33D097",
                    moderate: "#38BDF8",
                    watch: "#F5A524"
                }
            },
            fontFamily: {
                sans: ["Inter", "sans-serif"],
                display: ["Space Grotesk", "Inter", "sans-serif"],
                mono: ["JetBrains Mono", "ui-monospace", "monospace"]
            },
            backgroundImage: {
                "brand-grad": "linear-gradient(92deg,#33D097 0%,#2DD4BF 55%,#38BDF8 110%)"
            },
            boxShadow: {
                glow: "0 6px 28px rgba(51,208,151,.32)",
                card: "0 24px 60px rgba(0,0,0,.45)"
            },
            keyframes: {
                "fade-in-up": {
                    from: { opacity: "0", transform: "translateY(14px)" },
                    to: { opacity: "1", transform: "translateY(0)" }
                },
                shimmer: {
                    from: { backgroundPosition: "200% 0" },
                    to: { backgroundPosition: "-200% 0" }
                }
            },
            animation: {
                "fade-in-up": "fade-in-up .5s cubic-bezier(.2,.7,.3,1) both",
                shimmer: "shimmer 1.8s linear infinite"
            }
        }
    },
    plugins: []
};
