"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Send,
  Square,
  Clock,
  Zap,
  Layers,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  Sparkles,
  GitBranch,
  Quote,
} from "lucide-react";
import ChatPanel from "@/components/ChatPanel";
import ProvenanceTheatre from "@/components/ProvenanceTheatre";
import ModeIndicator from "@/components/ModeIndicator";
import RouterCard from "@/components/RouterCard";
import TopNav from "@/components/TopNav";
import PersonaPicker from "@/components/PersonaPicker";
import SuggestionBubbles from "@/components/SuggestionBubbles";
import {
  queryStreamAPI,
  recentHistory,
  type BlockRef,
  type PipelineStep,
  type QueryResponse,
} from "@/lib/api";
import { useQueryStore, selectHighlightedCite } from "@/lib/queryStore";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

function ValuePropRow() {
  const items = [
    {
      icon: Sparkles,
      title: "Routed by intent",
      body: "Each question is classified A / B / C and dispatched to a specialised retrieval pipeline.",
    },
    {
      icon: GitBranch,
      title: "Grounded in a graph",
      body: "Multi-hop entity reasoning over PolyU's link graph, not a flat vector haystack.",
    },
    {
      icon: Quote,
      title: "Every claim cited",
      body: "Inline [n] markers map back to the exact source block — hover to trace, click to read.",
    },
  ];
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-left max-w-3xl w-full">
      {items.map((it) => {
        const Icon = it.icon;
        return (
          <div
            key={it.title}
            className="rounded-lg border border-border bg-surface-alt/60 p-4"
          >
            <div className="flex items-center gap-2 mb-2">
              <Icon size={14} className="text-primary" />
              <span className="text-[11px] font-mono uppercase tracking-[0.14em] text-text-muted">
                {it.title}
              </span>
            </div>
            <p className="text-[13px] leading-relaxed text-text-main">
              {it.body}
            </p>
          </div>
        );
      })}
    </div>
  );
}

function EmptyRightPanel() {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center px-6">
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.2, 0.8, 0.2, 1] }}
      >
        <div className="mb-5 relative">
          <div className="w-14 h-14 rounded-xl bg-surface-alt border border-border flex items-center justify-center mx-auto">
            <Layers size={24} className="text-text-muted" />
          </div>
        </div>
        <p className="text-sm font-medium text-text-main mb-1.5">
          Retrieval Pipeline
        </p>
        <p className="text-xs text-text-muted leading-relaxed max-w-[220px]">
          Send a question to watch the router pick a mode, search the graph,
          and stream an answer back step by step.
        </p>
        <div className="mt-5 flex flex-col gap-1.5 text-[10px] font-mono text-text-muted items-start mx-auto w-fit">
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-accent/50" />
            Query Routing
          </span>
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-primary/45" />
            Vector Search
          </span>
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-success/50" />
            Graph Traversal
          </span>
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-accent-soft/70" />
            LLM Generation
          </span>
        </div>
      </motion.div>
    </div>
  );
}

export default function HomePage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [lastResponse, setLastResponse] = useState<QueryResponse | null>(null);
  const [liveAnswer, setLiveAnswer] = useState("");
  const [rightPanelOpen, setRightPanelOpen] = useState(true);
  const [routerCardOpen, setRouterCardOpen] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // Token coalescing: tokens arrive faster than the browser can paint, and each
  // one re-clones `messages` + re-parses the whole answer through ReactMarkdown.
  // We buffer the latest accumulated text and flush it to React state at most
  // once per animation frame, so rendering stays smooth instead of stuttering.
  const pendingAnswerRef = useRef<string | null>(null);
  const rafRef = useRef<number | null>(null);

  // store slice — granular selectors keep rerenders local
  const isLoading = useQueryStore((s) => s.isStreaming);
  const steps = useQueryStore((s) => s.steps);
  const blocks = useQueryStore((s) => s.blocks);
  const routing = useQueryStore((s) => s.routing);
  const cacheHit = useQueryStore((s) => s.cacheHit);
  const currentStage = useQueryStore((s) => s.currentStage);
  const suggestions = useQueryStore((s) => s.suggestions);
  const highlightedCitation = useQueryStore(selectHighlightedCite);
  const selectedCitation = useQueryStore((s) => s.selectedCite);

  const beginStream = useQueryStore((s) => s.beginStream);
  const endStream = useQueryStore((s) => s.endStream);
  const setRouting = useQueryStore((s) => s.setRouting);
  const pushStep = useQueryStore((s) => s.pushStep);
  const setBlocksAct = useQueryStore((s) => s.setBlocks);
  const setCacheHitAct = useQueryStore((s) => s.setCacheHit);
  const setElapsed = useQueryStore((s) => s.setElapsed);
  const setSuggestionsAct = useQueryStore((s) => s.setSuggestions);
  const setHoveredCite = useQueryStore((s) => s.setHoveredCite);
  const setSelectedCite = useQueryStore((s) => s.setSelectedCite);
  const setStage = useQueryStore((s) => s.setStage);
  const resetStore = useQueryStore((s) => s.reset);

  // Apply the latest buffered answer text to React state. Updating `messages`
  // and `liveAnswer` together in one pass keeps the assistant bubble and the
  // scroll effect in sync while only paying the ReactMarkdown re-parse cost
  // once per frame.
  const flushPendingAnswer = useCallback(() => {
    rafRef.current = null;
    const text = pendingAnswerRef.current;
    if (text === null) return;
    pendingAnswerRef.current = null;
    setLiveAnswer(text);
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== "assistant" || last.content === text) return prev;
      const next = [...prev];
      next[next.length - 1] = { role: "assistant", content: text };
      return next;
    });
  }, []);

  // Buffer a token and schedule (at most) one flush per animation frame.
  const scheduleAnswer = useCallback(
    (text: string) => {
      pendingAnswerRef.current = text;
      if (rafRef.current === null) {
        rafRef.current = requestAnimationFrame(flushPendingAnswer);
      }
    },
    [flushPendingAnswer]
  );

  // Cancel any pending frame on unmount so a late flush can't touch a
  // torn-down tree.
  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, liveAnswer]);

  // The zustand query store lives at module scope, so its routing/steps/blocks
  // survive route changes — but the page's local `messages`/`lastResponse`
  // reset every remount. That mismatch is what made the right panel show a
  // stale pipeline + page + blocks after navigating away and back. Reset the
  // store on mount whenever the chat itself is empty so the new landing is
  // genuinely blank.
  useEffect(() => {
    if (messages.length === 0 && !isLoading) {
      resetStore();
    }
    // Run once on mount; later resets are handled by handleNewChat.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSubmit = async (override?: string) => {
    const raw = (override ?? input).trim();
    if (!raw || isLoading) return;

    const userMsg = raw;
    setInput("");
    // Derive the history *before* we push the new user turn into `messages`
    // — the contextualizer should see only completed prior pairs.
    const history = recentHistory(messages, 3);
    setMessages((prev) => [...prev, { role: "user", content: userMsg }]);
    setLastResponse(null);
    setLiveAnswer("");
    beginStream();

    setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    let collected = "";
    let collectedBlocks: BlockRef[] = [];
    let collectedTrace: PipelineStep[] = [];
    let mode = "";
    let elapsed = 0;

    try {
      await queryStreamAPI(
        userMsg,
        undefined,
        {
          onRouting: (data) => {
            mode = data.mode;
            setRouting({
              mode: data.mode,
              alt_mode: data.alt_mode,
              reasoning: data.reasoning,
              confidence: data.confidence,
            });
          },
          onRetrievalStep: (step) => {
            collectedTrace = [...collectedTrace, step];
            pushStep(step);
          },
          onBlocks: (incoming) => {
            collectedBlocks = incoming;
            setBlocksAct(incoming);
          },
          onCache: ({ hit }) => {
            setCacheHitAct(hit);
          },
          onToken: (text) => {
            collected += text;
            scheduleAnswer(collected);
          },
          onSuggestions: (items) => {
            setSuggestionsAct(items, userMsg);
          },
          onDone: (data) => {
            // Drop any buffered frame — we're about to write the authoritative
            // final answer, and a late flush would clobber it with stale text.
            if (rafRef.current !== null) {
              cancelAnimationFrame(rafRef.current);
              rafRef.current = null;
            }
            pendingAnswerRef.current = null;
            elapsed = data.elapsed_seconds;
            setElapsed(elapsed);
            const finalAnswer = data.answer || collected;
            setLiveAnswer(finalAnswer);
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === "assistant") {
                next[next.length - 1] = { role: "assistant", content: finalAnswer };
              }
              return next;
            });
            setLastResponse({
              answer: finalAnswer,
              mode: mode || "mode_a",
              routing_reasoning: "",
              blocks: collectedBlocks,
              elapsed_seconds: elapsed,
              pipeline_trace: collectedTrace,
            });
          },
          onError: (detail) => {
            setMessages((prev) => {
              const next = [...prev];
              next[next.length - 1] = {
                role: "assistant",
                content:
                  "Sorry, an error occurred while processing your query: " + detail,
              };
              return next;
            });
          },
        },
        ctrl.signal,
        history
      );
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setMessages((prev) => {
          const next = [...prev];
          next[next.length - 1] = {
            role: "assistant",
            content:
              "Sorry, an error occurred while processing your query. Please make sure the backend API is running.",
          };
          return next;
        });
      }
    } finally {
      // Stop any buffered token frame from landing after the stream ends
      // (error / abort / normal completion all funnel through here).
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      pendingAnswerRef.current = null;
      endStream();
      abortRef.current = null;
    }
  };

  const handleStop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  const handleBubblePick = useCallback(
    (q: string) => {
      if (isLoading) return;
      handleSubmit(q);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isLoading]
  );

  const handleCitationClick = useCallback(
    (idx: number) => {
      setSelectedCite(idx);
      setStage("page");
    },
    [setSelectedCite, setStage]
  );

  const handleCitationHover = useCallback(
    (idx: number | null) => {
      setHoveredCite(idx);
    },
    [setHoveredCite]
  );

  const handleNewChat = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setMessages([]);
    setLastResponse(null);
    setLiveAnswer("");
    setInput("");
    resetStore();
    inputRef.current?.focus();
  }, [resetStore]);

  const hasRightContent =
    isLoading || lastResponse !== null || steps.length > 0;
  const traceForPanel =
    isLoading || steps.length > 0
      ? steps
      : lastResponse?.pipeline_trace || [];
  const blocksForPanel =
    isLoading || blocks.length > 0 ? blocks : lastResponse?.blocks || [];
  const modeForBadge = lastResponse?.mode || routing?.mode || null;
  const isEmpty = messages.length === 0 && !isLoading;

  const trailing = (
    <div className="flex items-center gap-2">
      {modeForBadge && (
        <div className="relative">
          <button
            type="button"
            data-router-card-trigger
            onClick={() => setRouterCardOpen((v) => !v)}
            aria-expanded={routerCardOpen}
            aria-haspopup="dialog"
            title="Show router decision"
            className="inline-flex items-center rounded-md px-1.5 py-1 hover:bg-surface-alt transition-colors focus:outline-none focus:ring-2 focus:ring-primary/30"
          >
            <ModeIndicator mode={modeForBadge} />
          </button>
          <RouterCard
            mode={modeForBadge}
            routing={routing}
            open={routerCardOpen}
            onOpenChange={setRouterCardOpen}
          />
        </div>
      )}
      {cacheHit !== null && (
        <span
          className={`text-[10px] font-mono px-2 py-0.5 rounded border ${
            cacheHit
              ? "bg-success/15 border-success/40 text-success"
              : "bg-surface-alt border-border text-text-muted"
          }`}
          title={cacheHit ? "Answer served from LLM cache" : "Fresh LLM call"}
        >
          {cacheHit ? "cache hit" : "live"}
        </span>
      )}
      {(messages.length > 0 || isLoading) && (
        <button
          onClick={handleNewChat}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border border-border bg-surface-alt hover:bg-surface-sunk hover:border-border-strong transition-colors text-xs font-medium text-text-main"
          title="Start a new conversation"
        >
          <Plus size={12} />
          <span className="hidden sm:inline">New chat</span>
        </button>
      )}
      <button
        onClick={() => setRightPanelOpen(!rightPanelOpen)}
        className="p-1.5 rounded-md hover:bg-surface-alt transition-colors text-text-muted hover:text-primary hidden md:flex"
        title={rightPanelOpen ? "Collapse panel" : "Expand panel"}
      >
        {rightPanelOpen ? (
          <PanelRightClose size={16} />
        ) : (
          <PanelRightOpen size={16} />
        )}
      </button>
    </div>
  );

  return (
    <div className="h-screen flex flex-col">
      <TopNav trailing={trailing} />

      {/* Main split layout */}
      <div className="flex-1 overflow-hidden flex flex-col md:flex-row">
        {/* Left panel: Chat */}
        <div
          className={`flex-1 flex flex-col min-w-0 transition-[width] duration-300 ease-brand ${
            rightPanelOpen ? "md:w-[55%] md:max-w-[55%]" : "w-full"
          }`}
        >
          <div ref={scrollRef} className="flex-1 overflow-y-auto paper-grain">
            {isEmpty ? (
              <EmptyHero
                onPick={(q) => {
                  setInput(q);
                  inputRef.current?.focus();
                }}
              />
            ) : (
              <div className="px-4 sm:px-6">
                <ChatPanel
                  messages={messages}
                  isLoading={isLoading && liveAnswer.length === 0}
                  isStreaming={isLoading}
                  onCitationClick={handleCitationClick}
                  onCitationHover={handleCitationHover}
                />
              </div>
            )}
          </div>

          {/* Composer */}
          <div className="flex-none border-t border-border bg-surface">
            <div className="max-w-3xl mx-auto px-4 sm:px-6 py-4">
              {/* Follow-up suggestions — only after a turn, never mid-stream */}
              {messages.length > 0 && !isLoading && suggestions.length > 0 && (
                <div className="mb-3">
                  <SuggestionBubbles
                    items={suggestions}
                    onPick={handleBubblePick}
                    disabled={isLoading}
                  />
                </div>
              )}
              <div className="relative">
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      handleSubmit();
                    }
                  }}
                  rows={1}
                  placeholder={
                    isEmpty
                      ? "Ask anything about PolyU — programmes, faculty, research…"
                      : "Follow up…"
                  }
                  className="w-full resize-none px-4 py-3 pr-14 rounded-xl border border-border bg-surface-alt text-[15px] text-text-main placeholder-text-muted focus:outline-none focus:border-primary/50 focus:ring-2 focus:ring-primary/15 transition-all leading-relaxed"
                  style={{ minHeight: "52px", maxHeight: "180px" }}
                />
                {isLoading ? (
                  <button
                    onClick={handleStop}
                    className="absolute right-2 bottom-2 p-2 rounded-lg bg-surface-alt border border-border-strong text-text-main hover:bg-surface-sunk hover:border-text-muted transition-colors"
                    aria-label="Stop generating"
                    title="Stop generating"
                  >
                    <Square size={13} fill="currentColor" />
                  </button>
                ) : (
                  <button
                    onClick={() => handleSubmit()}
                    disabled={!input.trim()}
                    className="absolute right-2 bottom-2 p-2 rounded-lg bg-primary text-text-inverse hover:bg-primary-deep disabled:opacity-40 disabled:hover:bg-primary transition-colors"
                    aria-label="Send"
                  >
                    <Send size={15} />
                  </button>
                )}
              </div>

              {lastResponse ? (
                <motion.div
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  className="mt-2 flex items-center gap-4 text-[11px] text-text-muted font-mono"
                >
                  <span className="flex items-center gap-1">
                    <Clock size={10} />
                    {lastResponse.elapsed_seconds}s
                  </span>
                  <span className="flex items-center gap-1">
                    <Zap size={10} />
                    {lastResponse.blocks.length} blocks
                  </span>
                  {lastResponse.entities_expanded && (
                    <span>{lastResponse.entities_expanded} entities explored</span>
                  )}
                </motion.div>
              ) : (
                <p className="mt-2 text-[11px] text-text-muted font-mono">
                  Press <kbd className="px-1 py-0.5 rounded bg-surface-alt border border-border text-[10px]">Enter</kbd> to send · <kbd className="px-1 py-0.5 rounded bg-surface-alt border border-border text-[10px]">Shift</kbd>+<kbd className="px-1 py-0.5 rounded bg-surface-alt border border-border text-[10px]">Enter</kbd> for newline
                </p>
              )}
            </div>
          </div>
        </div>

        {/* Right panel: Pipeline + Citations (desktop) */}
        <AnimatePresence>
          {rightPanelOpen && (
            <motion.div
              initial={{ width: 0, opacity: 0 }}
              animate={{ width: "45%", opacity: 1 }}
              exit={{ width: 0, opacity: 0 }}
              transition={{ duration: 0.25, ease: [0.2, 0.8, 0.2, 1] }}
              className="hidden md:flex flex-col border-l border-border bg-surface-alt/40 overflow-hidden"
            >
              {hasRightContent ? (
                <ProvenanceTheatre
                  steps={traceForPanel}
                  blocks={blocksForPanel}
                  highlightedIndex={highlightedCitation}
                  selectedIndex={selectedCitation}
                  isLoading={isLoading}
                  activeTab={currentStage}
                  onTabChange={setStage}
                  onCitationClick={handleCitationClick}
                  onCitationHover={handleCitationHover}
                />
              ) : (
                <EmptyRightPanel />
              )}
            </motion.div>
          )}
        </AnimatePresence>

        {/* Mobile bottom panel */}
        {hasRightContent && (
          <div className="md:hidden flex-none border-t border-border bg-surface max-h-[42vh] overflow-hidden flex flex-col">
            <ProvenanceTheatre
              steps={traceForPanel}
              blocks={blocksForPanel}
              highlightedIndex={highlightedCitation}
              selectedIndex={selectedCitation}
              isLoading={isLoading}
              activeTab={currentStage}
              onTabChange={setStage}
              onCitationClick={handleCitationClick}
              onCitationHover={handleCitationHover}
              compact
            />
          </div>
        )}
      </div>
    </div>
  );
}

function EmptyHero({ onPick }: { onPick: (q: string) => void }) {
  return (
    <div className="min-h-full flex items-center justify-center px-4 sm:px-8 py-12">
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: [0.2, 0.8, 0.2, 1] }}
        className="w-full max-w-3xl"
      >
        <h2 className="font-display text-[clamp(2rem,5vw,3.25rem)] font-bold text-text-main leading-[1.05] tracking-tight mb-3">
          PolyUQuest
        </h2>

        <p className="font-display text-[clamp(1.05rem,2.4vw,1.5rem)] leading-snug text-primary mb-6">
          Structure-Aware Retrieval-Augmented Generation
          <br className="hidden sm:block" /> over Web Heterogeneous Graphs
        </p>

        <p className="text-[15px] leading-relaxed text-text-muted max-w-2xl mb-8">
          Ask about PolyU — programmes, faculty, research, admissions. Every
          answer is routed by intent, retrieved from a structure-aware knowledge
          graph built from PolyU&apos;s own pages, and cited back to its source
          block.
        </p>

        <ValuePropRow />

        <div className="mt-10">
          <PersonaPicker onPick={onPick} />
        </div>
      </motion.div>
    </div>
  );
}
