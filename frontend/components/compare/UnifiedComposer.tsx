"use client";

import { useRef } from "react";
import { Send, ChevronRight } from "lucide-react";

export interface PresetCase {
  hop: "1-hop" | "2-hop";
  label: string;
  q: string;
  why: string;
}

interface Props {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  isLoading: boolean;
  presets: PresetCase[];
  onPickPreset: (preset: PresetCase) => void;
}

export default function UnifiedComposer({
  value,
  onChange,
  onSubmit,
  isLoading,
  presets,
  onPickPreset,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  return (
    <div className="flex-none border-t border-border bg-surface">
      <div className="px-4 sm:px-6 py-3.5">
        {/* Preset row */}
        <div className="flex items-center gap-2 mb-3 overflow-x-auto">
          <span className="text-[10px] font-mono uppercase tracking-[0.16em] text-text-muted shrink-0">
            Preset
          </span>
          {presets.map((p) => {
            const active = p.q === value;
            return (
              <button
                key={p.q}
                onClick={() => {
                  onPickPreset(p);
                  textareaRef.current?.focus();
                }}
                className={`group flex items-center gap-2 px-2.5 py-1 rounded-md border transition-all whitespace-nowrap ${
                  active
                    ? "border-primary/60 bg-primary/[0.06] text-text-main"
                    : "border-border bg-surface hover:border-border-strong text-text-muted hover:text-text-main"
                }`}
                title={p.why}
              >
                <span
                  className={`text-[9.5px] font-mono uppercase tracking-[0.14em] px-1 py-0.5 rounded ${
                    p.hop === "1-hop"
                      ? "bg-success/15 text-success"
                      : "bg-primary/15 text-primary"
                  }`}
                >
                  {p.hop}
                </span>
                <span className="text-[11.5px] font-medium">{p.label}</span>
                <ChevronRight
                  size={11}
                  className="text-text-muted opacity-0 group-hover:opacity-100 transition-opacity"
                />
              </button>
            );
          })}
        </div>

        {/* Composer */}
        <div className="relative">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSubmit();
              }
            }}
            rows={1}
            placeholder="Ask the same question to all three systems…"
            className="w-full resize-none pl-4 pr-32 py-3 rounded-xl border border-border bg-surface-alt text-[14.5px] text-text-main placeholder-text-muted focus:outline-none focus:border-primary/50 focus:ring-2 focus:ring-primary/15 transition-all"
            style={{ minHeight: "52px", maxHeight: "160px" }}
          />
          <button
            onClick={onSubmit}
            disabled={!value.trim() || isLoading}
            className="absolute right-2 bottom-2 flex items-center gap-1.5 px-3 py-2 rounded-lg bg-primary text-text-inverse hover:bg-primary-deep disabled:opacity-40 disabled:hover:bg-primary transition-colors text-xs font-medium"
          >
            <Send size={13} />
            <span>Run all 3</span>
          </button>
        </div>
        <p className="mt-1.5 text-[10.5px] text-text-muted font-mono">
          Ours streams live · LightRAG &amp; HTMLRAG replay pre-recorded JSON
        </p>
      </div>
    </div>
  );
}
