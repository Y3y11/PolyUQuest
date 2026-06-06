"use client";

import { useMemo, useState } from "react";
import { motion } from "framer-motion";
import {
  ArrowRight,
  GraduationCap,
  BookOpen,
  Briefcase,
  Compass,
} from "lucide-react";

export type Persona =
  | "prospective"
  | "current"
  | "faculty"
  | "browsing";

export type Category =
  | "admissions"
  | "academics"
  | "faculty"
  | "research"
  | "campus_life";

const PERSONAS: {
  id: Persona;
  label: string;
  short: string;
  icon: typeof GraduationCap;
  categories: Category[];
}[] = [
  {
    id: "prospective",
    label: "Prospective student",
    short: "Prospective",
    icon: GraduationCap,
    categories: ["admissions", "academics"],
  },
  {
    id: "current",
    label: "Current student",
    short: "Current",
    icon: BookOpen,
    categories: ["academics", "faculty"],
  },
  {
    id: "faculty",
    label: "Faculty / researcher",
    short: "Faculty",
    icon: Briefcase,
    categories: ["faculty", "research"],
  },
  {
    id: "browsing",
    label: "Just browsing",
    short: "Browsing",
    icon: Compass,
    categories: ["admissions", "academics", "faculty", "research"],
  },
];

const CATEGORY_LABEL: Record<Category, string> = {
  admissions: "Admissions",
  academics: "Academics",
  faculty: "Faculty",
  research: "Research & Departments",
  campus_life: "Campus life",
};

const CATEGORY_DOT: Record<Category, string> = {
  admissions: "bg-accent",
  academics: "bg-primary",
  faculty: "bg-success",
  research: "bg-accent-soft",
  campus_life: "bg-border-strong",
};

interface Starter {
  q: string;
  category: Category;
}

// Each category gets a few crawl-backed starters. The persona only changes
// *which* categories are highlighted — starters always come from these
// fixed pools, so we never recommend a question whose pages don't exist.
const STARTERS_BY_CATEGORY: Record<Category, Starter[]> = {
  admissions: [
    {
      q: "What are the admission requirements for MSc Data Science and Analytics?",
      category: "admissions",
    },
    {
      q: "What is the tuition for MSc AI and Big Data Analytics for non-local students?",
      category: "admissions",
    },
    {
      q: "What scholarships are available for international undergraduate students?",
      category: "admissions",
    },
  ],
  academics: [
    {
      q: "What is the credit requirement to graduate from BSc Computing?",
      category: "academics",
    },
    {
      q: "Does the BSc Computing programme require a portfolio?",
      category: "academics",
    },
    {
      q: "What is the deferment procedure for undergraduate students?",
      category: "academics",
    },
  ],
  faculty: [
    {
      q: "Who teaches the Machine Learning subject in COMP?",
      category: "faculty",
    },
    {
      q: "Which professors in COMP do NLP research?",
      category: "faculty",
    },
    {
      q: "What is Prof. Cao Jiannong's research direction?",
      category: "faculty",
    },
  ],
  research: [
    {
      q: "What does the COMP department research focus on?",
      category: "research",
    },
    {
      q: "Which department runs the Smart Cities Research Institute?",
      category: "research",
    },
    {
      q: "List the major research areas of the Faculty of Engineering.",
      category: "research",
    },
  ],
  campus_life: [],
};

function pickStartersForPersona(persona: Persona): Starter[] {
  const p = PERSONAS.find((x) => x.id === persona)!;
  if (persona === "browsing") {
    // one starter from each highlighted category
    return p.categories
      .map((c) => STARTERS_BY_CATEGORY[c][0])
      .filter(Boolean);
  }
  // 3 starters: weight the first highlighted category 2 : 1
  const [primary, secondary] = p.categories;
  return [
    STARTERS_BY_CATEGORY[primary][0],
    STARTERS_BY_CATEGORY[primary][1],
    STARTERS_BY_CATEGORY[secondary][0],
  ].filter(Boolean);
}

interface Props {
  onPick: (q: string) => void;
}

export default function PersonaPicker({ onPick }: Props) {
  const [persona, setPersona] = useState<Persona>("browsing");
  const starters = useMemo(() => pickStartersForPersona(persona), [persona]);
  const activePersona = PERSONAS.find((p) => p.id === persona)!;

  return (
    <div className="w-full">
      {/* Persona pill row */}
      <div className="flex items-center gap-2 overflow-x-auto pb-1 -mx-1 px-1">
        <span className="text-[10px] font-mono uppercase tracking-[0.16em] text-text-muted shrink-0 mr-1">
          I am a
        </span>
        {PERSONAS.map((p) => {
          const Icon = p.icon;
          const active = p.id === persona;
          return (
            <button
              key={p.id}
              onClick={() => setPersona(p.id)}
              className={`inline-flex items-center gap-1.5 shrink-0 px-3 py-1.5 rounded-full border text-[12px] font-medium transition-all ${
                active
                  ? "border-primary/60 bg-primary/[0.08] text-primary"
                  : "border-border bg-surface text-text-muted hover:text-text-main hover:border-border-strong"
              }`}
              aria-pressed={active}
            >
              <Icon size={12} />
              <span className="hidden sm:inline">{p.label}</span>
              <span className="sm:hidden">{p.short}</span>
            </button>
          );
        })}
      </div>

      {/* Category chip row — read-only, reflects persona's highlighted categories */}
      <div className="mt-3 flex items-center gap-1.5 flex-wrap text-[11px]">
        <span className="text-[10px] font-mono uppercase tracking-[0.14em] text-text-muted">
          Focus
        </span>
        {(["admissions", "academics", "faculty", "research", "campus_life"] as Category[]).map(
          (c) => {
            const highlighted = activePersona.categories.includes(c);
            const disabled = c === "campus_life";
            return (
              <span
                key={c}
                className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border ${
                  disabled
                    ? "border-dashed border-border text-text-muted/70 italic"
                    : highlighted
                    ? "border-border-strong bg-surface text-text-main"
                    : "border-border bg-surface text-text-muted"
                }`}
                title={disabled ? "Coming after the next crawl" : undefined}
              >
                <span
                  className={`w-1.5 h-1.5 rounded-full ${CATEGORY_DOT[c]} ${
                    disabled ? "opacity-40" : ""
                  }`}
                />
                {CATEGORY_LABEL[c]}
                {disabled && (
                  <span className="text-[9px] font-mono uppercase tracking-[0.14em] text-text-muted">
                    soon
                  </span>
                )}
              </span>
            );
          }
        )}
      </div>

      {/* Starter grid */}
      <div className="mt-5">
        <p className="text-[11px] font-mono uppercase tracking-[0.14em] text-text-muted mb-2.5">
          Try a starter
        </p>
        <motion.div
          key={persona}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
          className="grid grid-cols-1 sm:grid-cols-3 gap-2.5"
        >
          {starters.map((s) => (
            <button
              key={s.q}
              onClick={() => onPick(s.q)}
              className="group relative text-left p-4 rounded-lg border border-border bg-surface hover:border-primary/40 hover:bg-surface-alt transition-all"
            >
              <div className="flex items-center gap-2 mb-2">
                <span
                  className={`w-1.5 h-1.5 rounded-full ${CATEGORY_DOT[s.category]}`}
                />
                <span className="text-[10px] font-mono uppercase tracking-[0.16em] text-text-muted">
                  {CATEGORY_LABEL[s.category]}
                </span>
              </div>
              <p className="text-[13px] text-text-main leading-snug pr-5 group-hover:text-primary transition-colors">
                {s.q}
              </p>
              <ArrowRight
                size={13}
                className="absolute right-3 bottom-3 text-text-muted opacity-0 group-hover:opacity-100 group-hover:translate-x-0.5 transition-all"
              />
            </button>
          ))}
        </motion.div>
      </div>
    </div>
  );
}
