"use client";

import { useEffect, useState } from "react";
import { Moon, Sun, Camera } from "lucide-react";

type Theme = "light" | "dark" | "paper";

// light → dark → paper → light. "paper" is the figure-capture theme: pure
// white background + neutral chrome (only the logo stays PolyU red) so graph
// screenshots drop cleanly into the paper.
const ORDER: Theme[] = ["light", "dark", "paper"];

const META: Record<Theme, { Icon: typeof Sun; label: string }> = {
  light: { Icon: Sun, label: "Light" },
  dark: { Icon: Moon, label: "Dark" },
  paper: { Icon: Camera, label: "Paper (figure capture)" },
};

export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("light");

  useEffect(() => {
    const stored = localStorage.getItem("theme") as Theme | null;
    if (stored && ORDER.includes(stored)) {
      setTheme(stored);
      document.documentElement.setAttribute("data-theme", stored);
    }
  }, []);

  const cycle = () => {
    const next = ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length];
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
  };

  const { Icon, label } = META[theme];
  const nextLabel = META[ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length]].label;

  return (
    <button
      onClick={cycle}
      className="p-2 rounded-lg hover:bg-surface-alt transition-colors"
      aria-label={`Theme: ${label}. Click to switch to ${nextLabel}.`}
      title={`Theme: ${label} — click for ${nextLabel}`}
    >
      <Icon size={18} />
    </button>
  );
}
