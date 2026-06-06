import type { Config } from "tailwindcss";

function cssVar(name: string) {
  return `rgb(var(--color-${name}) / <alpha-value>)`;
}

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
  ],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        surface: cssVar("surface"),
        "surface-alt": cssVar("surface-alt"),
        "surface-sunk": cssVar("surface-sunk"),
        primary: cssVar("primary"),
        "primary-deep": cssVar("primary-deep"),
        accent: cssVar("accent"),
        "accent-soft": cssVar("accent-soft"),
        "text-main": cssVar("text-main"),
        "text-muted": cssVar("text-muted"),
        "text-inverse": cssVar("text-inverse"),
        border: cssVar("border"),
        "border-strong": cssVar("border-strong"),
        critical: cssVar("critical"),
        success: cssVar("success"),
      },
      fontFamily: {
        display: ["var(--font-display)", "serif"],
        body: ["var(--font-body)", "sans-serif"],
        mono: ["var(--font-mono)", "monospace"],
      },
      borderRadius: {
        sm: "4px",
        DEFAULT: "6px",
        md: "8px",
        lg: "10px",
        xl: "14px",
        "2xl": "18px",
      },
      boxShadow: {
        "soft-1": "0 1px 2px rgb(0 0 0 / 0.04), 0 2px 8px rgb(0 0 0 / 0.04)",
        "paper-2": "0 4px 14px rgb(0 0 0 / 0.06), 0 1px 3px rgb(0 0 0 / 0.08)",
        "brick-glow": "0 0 0 3px rgb(var(--color-primary) / 0.18)",
      },
      transitionTimingFunction: {
        brand: "cubic-bezier(.2,.8,.2,1)",
      },
      animation: {
        "fade-in": "fadeIn 380ms cubic-bezier(.2,.8,.2,1) forwards",
        "slide-up": "slideUp 380ms cubic-bezier(.2,.8,.2,1) forwards",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        slideUp: {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [],
};

export default config;
