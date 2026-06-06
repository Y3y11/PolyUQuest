"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Network, MessagesSquare } from "lucide-react";
import ThemeToggle from "@/components/ThemeToggle";

// Compare is hidden from the live demo nav — the page itself still exists
// at /compare for internal use, but we don't surface it.
const NAV = [
  { href: "/", label: "Ask", icon: MessagesSquare, match: (p: string) => p === "/" },
  {
    href: "/graph",
    label: "Graph",
    icon: Network,
    match: (p: string) => p.startsWith("/graph"),
  },
];

export default function TopNav({ trailing }: { trailing?: React.ReactNode }) {
  const pathname = usePathname() || "/";

  return (
    <header className="flex-none border-b border-border bg-surface/85 backdrop-blur-md z-20">
      {/* 3-column grid keeps the centered nav anchored regardless of trailing
          width — `justify-between` was reflowing the nav on every route swap. */}
      <div className="px-4 sm:px-6 h-12 grid grid-cols-[1fr_auto_1fr] items-center gap-4">
        <Link
          href="/"
          className="flex items-center gap-2.5 group justify-self-start min-w-0"
          title="PolyUQuest — Structure-Aware Retrieval-Augmented Generation over Web Heterogeneous Graphs"
        >
          <span className="brand-mark">P</span>
          <span className="font-display text-[15px] font-semibold tracking-tight text-text-main">
            PolyUQuest
          </span>
        </Link>

        <nav className="flex items-center gap-1 sm:gap-3 justify-self-center">
          {NAV.map((item) => {
            const active = item.match(pathname);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                data-active={active}
                className={`nav-link flex items-center gap-1.5 text-sm font-medium transition-colors ${
                  active ? "text-primary" : "text-text-muted hover:text-text-main"
                }`}
              >
                <Icon size={14} />
                <span>{item.label}</span>
              </Link>
            );
          })}
        </nav>

        <div className="flex items-center gap-2 justify-self-end min-w-0">
          {trailing}
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
