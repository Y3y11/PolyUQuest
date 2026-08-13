"use client";

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Search,
  Info,
  AlertCircle,
  ZoomIn,
  ZoomOut,
  Maximize2,
  RefreshCw,
  Download,
  GitBranch,
  X,
  ChevronDown,
  ChevronRight,
  Layers,
  Compass,
  Tag,
} from "lucide-react";
import TopNav from "@/components/TopNav";
import {
  getGraphData,
  getGraphStats,
  getGraphNeighbors,
  getGraphPath,
  getGraphLayeredSlice,
  type GraphData,
  type GraphStats,
  type GraphNode,
  type GraphEdge,
} from "@/lib/api";
import GraphCanvas, {
  type GraphCanvasHandle,
  type PathHighlight,
  humanNodeTypeLabel,
} from "@/components/GraphCanvas";

// Brick-aligned palette — driven by CSS custom properties so it tracks theme.
const NODE_TYPE_COLORS: Record<string, string> = {
  WebPage: "rgb(var(--node-webpage))",
  Entity: "rgb(var(--node-entity))",
  Block: "rgb(var(--node-block))",
  TopicKeyword: "rgb(var(--node-topic))",
};

const ALL_TYPES = ["WebPage", "Entity", "Block", "TopicKeyword"];

const HUMAN_TYPE_FOR_LEGEND: Record<string, string> = {
  WebPage: "Page",
  Block: "Snippet",
  Entity: "Entity",
  TopicKeyword: "Topic",
};

// Three curated anchors. These are the slices the layered view defaults to —
// each one is known to have a dense, readable WebPage → Blocks → Entities
// subtree in the current crawl, so the demo lands on a real story not a stub.
interface SliceTab {
  id: string;
  label: string;
  anchor: string;
  caption: string;
}

const SLICE_TABS: SliceTab[] = [
  {
    id: "programme",
    label: "Programme example",
    // "BSc (Hons)" is the most specific substring that uniquely resolves to
    // the PolyU COMP Artificial Intelligence and Data Analytics (AIDA)
    // programme page via the API's substring-on-entity-name fallback.
    anchor: "BSc (Hons)",
    caption:
      "PolyU COMP AIDA — undergraduate programme page sliced into blocks and entities",
  },
  {
    id: "department",
    label: "Department example",
    anchor: "Department of Computing",
    caption:
      "Department of Computing — department page sliced into blocks and entities",
  },
  {
    id: "professor",
    label: "Professor example",
    anchor: "Jiannong",
    caption:
      "Prof. Cao Jiannong — research-staff page sliced into blocks and entities",
  },
];

function edgeKey(src: string, tgt: string) {
  return `${src}__${tgt}`;
}

function mergeGraph(prev: GraphData | null, add: GraphData): GraphData {
  if (!prev) return add;
  const nodeIds = new Set(prev.nodes.map((n) => n.id));
  const edgeKeys = new Set(prev.edges.map((e) => edgeKey(e.source, e.target)));
  const nodes = [...prev.nodes];
  const edges = [...prev.edges];
  for (const n of add.nodes) {
    if (!nodeIds.has(n.id)) {
      nodes.push(n);
      nodeIds.add(n.id);
    }
  }
  for (const e of add.edges) {
    const k = edgeKey(e.source, e.target);
    if (!edgeKeys.has(k)) {
      edges.push(e);
      edgeKeys.add(k);
    }
  }
  return { nodes, edges };
}

const CROSS_LAYER_EDGE_TYPES = new Set([
  "CONTAINS",
  "EXTRACTED_FROM",
  "HAS_TOPIC",
]);

export default function GraphPage() {
  // Agent exploration writes WebPage/Block/Link first; entity enrichment may
  // run later. Start with the live cross-layer graph so newly acquired
  // knowledge is visible immediately instead of opening an entity-only demo
  // slice that can legitimately be empty on a fresh deployment.
  const [mode, setMode] = useState<"layered" | "force">("force");
  const [activeTab, setActiveTab] = useState<string>(SLICE_TABS[0].id);

  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [stats, setStats] = useState<GraphStats | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<GraphEdge | null>(null);
  const [visibleTypes, setVisibleTypes] = useState<Set<string>>(
    new Set(ALL_TYPES)
  );
  const [pathMode, setPathMode] = useState<{ source: string | null } | null>(
    null
  );
  const [highlightedPath, setHighlightedPath] = useState<PathHighlight | null>(
    null
  );
  const [pathError, setPathError] = useState<string | null>(null);
  const [searchEmpty, setSearchEmpty] = useState<string | null>(null);
  const [hoverInfo, setHoverInfo] = useState<{
    node: GraphNode;
    x: number;
    y: number;
  } | null>(null);
  const [expanding, setExpanding] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Node titles are off by default so the picture reads as shape + colour
  // clusters; toggling on shows each node's label for closer inspection.
  const [showLabels, setShowLabels] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const canvasRef = useRef<GraphCanvasHandle>(null);

  // Always fetch global stats once — they're shown in the collapsible
  // "Full graph stats" section in either mode.
  useEffect(() => {
    const controller = new AbortController();
    getGraphStats(controller.signal)
      .then((s) => setStats(s))
      .catch((err) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        console.error(err);
      });
    return () => controller.abort();
  }, []);

  // Load the active layered slice whenever the tab or mode changes.
  useEffect(() => {
    if (mode !== "layered") return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);
    setSelectedNode(null);
    setSelectedEdge(null);
    setHighlightedPath(null);
    setPathMode(null);

    const tab = SLICE_TABS.find((t) => t.id === activeTab) || SLICE_TABS[0];
    getGraphLayeredSlice(tab.anchor, 80, controller.signal)
      .then((data) => {
        if (data.nodes.length === 0) {
          setError(
            `No slice found for "${tab.anchor}". The anchor may not exist in the current crawl yet.`
          );
          setGraphData({ nodes: [], edges: [] });
        } else {
          setGraphData(data);
        }
      })
      .catch((err) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        console.error(err);
        setError(
          "Failed to load the layered slice. Make sure the backend API is running."
        );
      })
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, [mode, activeTab]);

  // First-time bootstrap of the free-explore view when user switches into it.
  // We lazy-load the default "all entities" graph only on demand so the
  // layered slice stays the cheap default.
  useEffect(() => {
    if (mode !== "force") return;
    if (graphData && graphData.nodes.length > 0) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);
    getGraphData(undefined, undefined, controller.signal)
      .then((data) => setGraphData(data))
      .catch((err) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        console.error(err);
        setError("Failed to load graph data. Make sure the backend API is running.");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
    // graphData is intentionally not in the dep array — we only want this to
    // run when entering force mode, not every time graphData changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setLoading(true);
    setError(null);
    setSearchEmpty(null);
    setSelectedNode(null);
    setSelectedEdge(null);
    setHighlightedPath(null);
    setPathMode(null);
    try {
      const data = await getGraphData(searchQuery, undefined, controller.signal);
      setGraphData(data);
      if (data.nodes.length === 0) {
        setSearchEmpty(`No page, block, entity, or topic matched "${searchQuery}".`);
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      console.error(err);
      setError("Search failed. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  const exitPathMode = useCallback(() => {
    setPathMode(null);
    setPathError(null);
  }, []);

  const togglePathMode = useCallback(() => {
    if (pathMode) {
      exitPathMode();
    } else {
      setPathMode({ source: null });
      setHighlightedPath(null);
      setPathError(null);
      setSelectedNode(null);
    }
  }, [pathMode, exitPathMode]);

  const runPath = useCallback(async (src: string, tgt: string) => {
    try {
      setPathError(null);
      const data = await getGraphPath(src, tgt);
      if (!data.nodes.length) {
        setPathError(`No path found between ${src} and ${tgt}`);
        return;
      }
      setGraphData((prev) => mergeGraph(prev, data));
      setHighlightedPath({
        nodes: new Set(data.nodes.map((n) => n.id)),
        edges: new Set(data.edges.map((e) => edgeKey(e.source, e.target))),
      });
    } catch (err) {
      console.error(err);
      setPathError("Path query failed");
    }
  }, []);

  const handleNodeClick = useCallback(
    (node: GraphNode) => {
      if (pathMode) {
        if (!pathMode.source) {
          setPathMode({ source: node.id });
          setPathError(null);
        } else if (pathMode.source !== node.id) {
          const src = pathMode.source;
          setPathMode(null);
          runPath(src, node.id);
        }
        return;
      }
      setSelectedEdge(null);
      setSelectedNode(node);
      setHighlightedPath(null);
    },
    [pathMode, runPath]
  );

  // Double-click neighbor expansion is meaningful in free explore mode only;
  // in layered mode the slice is intentionally self-contained.
  const handleNodeDoubleClick = useCallback(
    async (node: GraphNode) => {
      if (mode === "layered") return;
      setExpanding(true);
      try {
        const data = await getGraphNeighbors(node.id, 1);
        setGraphData((prev) => mergeGraph(prev, data));
      } catch (err) {
        console.error(err);
      } finally {
        setExpanding(false);
      }
    },
    [mode]
  );

  const handleEdgeClick = useCallback((edge: GraphEdge) => {
    setSelectedNode(null);
    setSelectedEdge(edge);
  }, []);

  const handleBackgroundClick = useCallback(() => {
    if (pathMode) return;
    setSelectedNode(null);
    setSelectedEdge(null);
    setHighlightedPath(null);
  }, [pathMode]);

  const handleNodeHover = useCallback(
    (node: GraphNode | null, pos?: { x: number; y: number }) => {
      if (node && pos) {
        setHoverInfo({ node, x: pos.x, y: pos.y });
      } else {
        setHoverInfo(null);
      }
    },
    []
  );

  const toggleType = (t: string) => {
    setVisibleTypes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });
  };

  // Per-slice numbers shown in the right sidebar — derived from the live
  // graphData rather than the global GraphStats, so they match what's on screen.
  const sliceCounts = useMemo(() => {
    const pages: GraphNode[] = [];
    const blocks: GraphNode[] = [];
    const entities: GraphNode[] = [];
    const topics: GraphNode[] = [];
    const entityByType: Record<string, number> = {};
    if (graphData) {
      for (const n of graphData.nodes) {
        if (n.type === "WebPage") pages.push(n);
        else if (n.type === "Block") blocks.push(n);
        else if (n.type === "TopicKeyword") topics.push(n);
        else if (n.type === "Entity") {
          entities.push(n);
          const et = (n.properties?.entity_type as string) || "OTHER";
          entityByType[et] = (entityByType[et] || 0) + 1;
        }
      }
    }
    let cross = 0;
    let same = 0;
    if (graphData) {
      for (const e of graphData.edges) {
        if (CROSS_LAYER_EDGE_TYPES.has((e.type || "").toUpperCase())) cross++;
        else same++;
      }
    }
    return { pages, blocks, entities, topics, entityByType, cross, same };
  }, [graphData]);

  const typeCounts = useMemo(() => {
    const m: Record<string, number> = {};
    if (graphData) {
      for (const n of graphData.nodes) m[n.type] = (m[n.type] || 0) + 1;
    }
    return m;
  }, [graphData]);

  const pathModeLabel = pathMode
    ? pathMode.source
      ? "Click the target node"
      : "Click the source node"
    : null;

  const activeTabObj = SLICE_TABS.find((t) => t.id === activeTab) || SLICE_TABS[0];

  const trailing =
    mode === "force" ? (
      <div className="relative hidden sm:block">
        <Search
          size={13}
          className="absolute left-3 top-1/2 -translate-y-1/2 text-text-muted"
        />
        <input
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          placeholder="Search graph…"
          className="pl-8 pr-3 py-1.5 rounded-md border border-border bg-surface-alt text-[13px] text-text-main placeholder-text-muted focus:outline-none focus:border-primary/50 focus:ring-2 focus:ring-primary/15 transition-all w-56"
        />
      </div>
    ) : null;

  return (
    <div className="h-screen flex flex-col">
      <TopNav trailing={trailing} />

      {/* Main */}
      <main className="flex-1 flex overflow-hidden">
        {/* Graph Canvas */}
        <div className="flex-1 relative flex flex-col">
          {/* Top strip: slice tabs (layered) OR mode switch (force) */}
          <div className="flex-none border-b border-border bg-surface/95 backdrop-blur-sm">
            <div className="flex items-center gap-2 px-4 py-2 overflow-x-auto">
              {mode === "layered" ? (
                <>
                  <span className="inline-flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-[0.16em] text-text-muted shrink-0">
                    <Layers size={11} className="text-accent" />
                    Three-layer slice
                  </span>
                  {SLICE_TABS.map((tab) => {
                    const active = tab.id === activeTab;
                    return (
                      <button
                        key={tab.id}
                        onClick={() => setActiveTab(tab.id)}
                        className={`inline-flex items-center gap-1.5 shrink-0 px-3 py-1.5 rounded-full border text-[12px] font-medium transition-all ${
                          active
                            ? "border-primary/60 bg-primary/[0.08] text-primary"
                            : "border-border bg-surface text-text-muted hover:text-text-main hover:border-border-strong"
                        }`}
                        aria-pressed={active}
                      >
                        {tab.label}
                      </button>
                    );
                  })}
                  <div className="ml-auto" />
                  <button
                    onClick={() => {
                      // Drop the layered slice's nodes before switching — the
                      // force-mode bootstrap effect bails when graphData is
                      // non-empty, so without this it would keep showing the
                      // ~few-dozen-node slice instead of fetching the full
                      // graph.
                      abortRef.current?.abort();
                      setGraphData(null);
                      setSelectedNode(null);
                      setSelectedEdge(null);
                      setHighlightedPath(null);
                      setSearchEmpty(null);
                      setError(null);
                      setMode("force");
                    }}
                    className="inline-flex items-center gap-1.5 shrink-0 px-3 py-1.5 rounded-full border border-border bg-surface text-text-muted hover:text-text-main hover:border-border-strong text-[12px]"
                    title="Switch to free-form exploration of the whole graph"
                  >
                    <Compass size={11} />
                    Free explore
                  </button>
                </>
              ) : (
                <>
                  <span className="inline-flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-[0.16em] text-text-muted shrink-0">
                    <Compass size={11} className="text-accent" />
                    Free explore
                  </span>
                  <span className="text-[12px] text-text-muted">
                    Search any page, block, entity, or topic; double-click to
                    expand neighbors, use the branch icon for shortest path.
                  </span>
                  <div className="ml-auto" />
                  <button
                    onClick={() => setMode("layered")}
                    className="inline-flex items-center gap-1.5 shrink-0 px-3 py-1.5 rounded-full border border-border bg-surface text-text-muted hover:text-text-main hover:border-border-strong text-[12px]"
                  >
                    <Layers size={11} />
                    Layered slice
                  </button>
                </>
              )}
            </div>
          </div>

          <div className="flex-1 relative">
            {loading ? (
              <div className="flex items-center justify-center h-full text-text-muted">
                <div className="text-center">
                  <div className="w-8 h-8 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-3" />
                  <p className="text-sm">Loading knowledge graph...</p>
                </div>
              </div>
            ) : error ? (
              <div className="flex items-center justify-center h-full text-text-muted">
                <div className="text-center max-w-md">
                  <AlertCircle size={32} className="mx-auto mb-3 text-critical" />
                  <p className="text-sm text-text-main">{error}</p>
                  <button
                    onClick={() => window.location.reload()}
                    className="mt-3 px-4 py-2 rounded-lg bg-primary text-text-inverse text-sm hover:bg-primary-deep transition-colors"
                  >
                    Retry
                  </button>
                </div>
              </div>
            ) : (
              <GraphCanvas
                ref={canvasRef}
                data={graphData}
                visibleTypes={mode === "force" ? visibleTypes : undefined}
                focusedNodeId={selectedNode?.id ?? null}
                pathModeActive={!!pathMode}
                highlightedPath={highlightedPath}
                layout={mode === "layered" ? "layered" : "force"}
                showLabels={showLabels}
                onNodeClick={handleNodeClick}
                onNodeDoubleClick={handleNodeDoubleClick}
                onEdgeClick={handleEdgeClick}
                onBackgroundClick={handleBackgroundClick}
                onNodeHover={handleNodeHover}
              />
            )}

            {/* Floating toolbar — top right */}
            {!loading && !error && (
              <div className="absolute top-4 right-4 flex flex-col gap-1.5 bg-surface/90 backdrop-blur-sm border border-border rounded-xl p-1.5">
                <ToolButton
                  label="Zoom in"
                  onClick={() => canvasRef.current?.zoomIn()}
                >
                  <ZoomIn size={16} />
                </ToolButton>
                <ToolButton
                  label="Zoom out"
                  onClick={() => canvasRef.current?.zoomOut()}
                >
                  <ZoomOut size={16} />
                </ToolButton>
                <ToolButton
                  label="Fit to view"
                  onClick={() => canvasRef.current?.fit()}
                >
                  <Maximize2 size={16} />
                </ToolButton>
                <ToolButton
                  label="Re-run layout"
                  onClick={() => canvasRef.current?.runLayout()}
                >
                  <RefreshCw size={16} />
                </ToolButton>
                <ToolButton
                  label={showLabels ? "Hide titles" : "Show titles"}
                  active={showLabels}
                  onClick={() => setShowLabels((v) => !v)}
                >
                  <Tag size={16} />
                </ToolButton>
                <ToolButton
                  label="Export graph (SVG, PNG fallback)"
                  onClick={() => canvasRef.current?.exportImage()}
                >
                  <Download size={16} />
                </ToolButton>
                {mode === "force" && (
                  <>
                    <div className="h-px bg-border my-0.5" />
                    <ToolButton
                      label={pathMode ? "Exit path mode" : "Find shortest path"}
                      onClick={togglePathMode}
                      active={!!pathMode}
                    >
                      <GitBranch size={16} />
                    </ToolButton>
                  </>
                )}
              </div>
            )}

            {/* Path mode banner */}
            {pathMode && (
              <div className="absolute top-4 left-1/2 -translate-x-1/2 flex items-center gap-2 px-3 py-2 rounded-lg bg-primary text-text-inverse text-xs font-medium shadow-lg">
                <GitBranch size={12} />
                <span>{pathModeLabel}</span>
                <button
                  onClick={exitPathMode}
                  className="ml-1 p-0.5 rounded hover:bg-black/10 transition-colors"
                  aria-label="Exit path mode"
                >
                  <X size={12} />
                </button>
              </div>
            )}

            {pathError && (
              <div className="absolute top-16 left-1/2 -translate-x-1/2 px-3 py-2 rounded-lg bg-critical text-text-inverse text-xs shadow-paper-2">
                {pathError}
              </div>
            )}

            {searchEmpty && !loading && (
              <div className="absolute top-16 left-1/2 -translate-x-1/2 flex items-center gap-2 px-3 py-2 rounded-lg bg-surface/95 backdrop-blur-sm border border-border text-xs text-text-main shadow-paper-2">
                <AlertCircle size={12} className="text-critical" />
                <span>{searchEmpty}</span>
                <button
                  onClick={() => setSearchEmpty(null)}
                  className="ml-1 p-0.5 rounded hover:bg-surface-alt text-text-muted"
                  aria-label="Dismiss"
                >
                  <X size={12} />
                </button>
              </div>
            )}

            {expanding && (
              <div className="absolute top-4 left-4 flex items-center gap-2 px-3 py-2 rounded-lg bg-surface/90 backdrop-blur-sm border border-border text-xs text-text-muted">
                <div className="w-3 h-3 border-2 border-primary border-t-transparent rounded-full animate-spin" />
                Expanding neighbors…
              </div>
            )}

            {/* Hover tooltip */}
            <AnimatePresence>
              {hoverInfo && (
                <motion.div
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.1 }}
                  className="absolute pointer-events-none z-10 px-3 py-2 rounded-lg bg-surface border border-border shadow-lg text-xs max-w-xs"
                  style={{
                    left: hoverInfo.x + 16,
                    top: hoverInfo.y + 16,
                  }}
                >
                  <div className="flex items-center gap-1.5 mb-1">
                    <span
                      className="w-2 h-2 rounded-sm"
                      style={{
                        background:
                          NODE_TYPE_COLORS[hoverInfo.node.type] || "#999",
                      }}
                    />
                    <span className="font-mono text-[10px] text-text-muted uppercase">
                      {humanNodeTypeLabel(hoverInfo.node)}
                    </span>
                  </div>
                  <p className="font-medium text-text-main break-words">
                    {hoverInfo.node.label}
                  </p>
                  {hoverInfo.node.properties?.description ? (
                    <p className="mt-1 text-text-muted text-[11px] leading-snug line-clamp-3">
                      {String(hoverInfo.node.properties.description)}
                    </p>
                  ) : null}
                  {mode === "force" ? (
                    <p className="mt-1.5 text-[10px] text-text-muted">
                      Double-click to expand neighbors
                    </p>
                  ) : null}
                </motion.div>
              )}
            </AnimatePresence>

            {/* Legend (free mode only — layered mode has the band structure as its legend) */}
            {mode === "force" && (
              <div className="absolute bottom-4 left-4 bg-surface/90 backdrop-blur-sm border border-border rounded-xl p-3">
                <div className="flex items-center justify-between mb-2">
                  <p className="text-[10px] text-text-muted font-mono">LEGEND</p>
                  {visibleTypes.size < ALL_TYPES.length && (
                    <button
                      onClick={() => setVisibleTypes(new Set(ALL_TYPES))}
                      className="text-[10px] text-primary hover:underline"
                    >
                      show all
                    </button>
                  )}
                </div>
                <div className="flex flex-col gap-1.5">
                  {ALL_TYPES.map((type) => {
                    const active = visibleTypes.has(type);
                    return (
                      <button
                        key={type}
                        onClick={() => toggleType(type)}
                        className={`flex items-center gap-2 text-left transition-opacity ${
                          active ? "opacity-100" : "opacity-40"
                        } hover:opacity-100`}
                      >
                        <span
                          className="w-3 h-3 rounded-sm flex-none"
                          style={{ background: NODE_TYPE_COLORS[type] }}
                        />
                        <span className="text-xs text-text-main">
                          {HUMAN_TYPE_FOR_LEGEND[type] || type}
                        </span>
                        {typeCounts[type] != null && (
                          <span className="text-[10px] text-text-muted font-mono">
                            {typeCounts[type]}
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Layer legend (layered mode only — node colour + size encode the
                three layers, since the layout is soft rather than strict rows). */}
            {mode === "layered" && !loading && !error && (
              <div className="absolute bottom-4 left-4 bg-surface/90 backdrop-blur-sm border border-border rounded-xl p-3">
                <p className="text-[10px] text-text-muted font-mono mb-2">
                  THREE LAYERS
                </p>
                <div className="flex flex-col gap-1.5 text-xs text-text-main">
                  <div className="flex items-center gap-2">
                    <span
                      className="w-3.5 h-3.5 rounded-sm flex-none"
                      style={{ background: NODE_TYPE_COLORS.WebPage }}
                    />
                    <span>Page</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span
                      className="w-2.5 h-2.5 rounded-sm flex-none"
                      style={{ background: NODE_TYPE_COLORS.Block }}
                    />
                    <span>Snippet</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span
                      className="w-2.5 h-2.5 rounded-sm flex-none"
                      style={{ background: NODE_TYPE_COLORS.Entity }}
                    />
                    <span>Entity</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span
                      className="w-2 h-2 rounded-sm flex-none"
                      style={{ background: NODE_TYPE_COLORS.TopicKeyword }}
                    />
                    <span>Topic</span>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Stats / Details Panel */}
        <motion.div
          initial={{ opacity: 0, x: 20 }}
          animate={{ opacity: 1, x: 0 }}
          className="w-80 flex-none border-l border-border bg-surface overflow-y-auto p-4"
        >
          {/* Selected node/edge details take priority */}
          {selectedNode ? (
            <DetailsPanel
              title="Selected Node"
              onClose={() => setSelectedNode(null)}
              accentColor={NODE_TYPE_COLORS[selectedNode.type]}
              typeLabel={humanNodeTypeLabel(selectedNode)}
              primary={selectedNode.label}
              subtitle={selectedNode.id}
              properties={selectedNode.properties}
              extra={
                mode === "force" ? (
                  <button
                    onClick={() => handleNodeDoubleClick(selectedNode)}
                    disabled={expanding}
                    className="w-full mt-3 px-3 py-2 rounded-lg bg-primary text-text-inverse text-xs font-medium hover:bg-primary-deep transition-colors disabled:opacity-50"
                  >
                    {expanding ? "Expanding…" : "Expand neighbors"}
                  </button>
                ) : null
              }
            />
          ) : selectedEdge ? (
            <DetailsPanel
              title="Selected Edge"
              onClose={() => setSelectedEdge(null)}
              accentColor="var(--color-accent)"
              typeLabel={humanEdgeLabel(selectedEdge.type)}
              primary={humanEdgeLabel(selectedEdge.type)}
              subtitle={`${selectedEdge.source} → ${selectedEdge.target}`}
              properties={selectedEdge.properties}
            />
          ) : mode === "layered" ? (
            <SliceSummary
              tab={activeTabObj}
              counts={sliceCounts}
              stats={stats}
            />
          ) : (
            <FreeExploreStats stats={stats} />
          )}
        </motion.div>
      </main>
    </div>
  );
}

const EDGE_TYPE_HUMAN: Record<string, string> = {
  CONTAINS: "contains",
  EXTRACTED_FROM: "mentioned in",
  HAS_TOPIC: "topic",
  LINKS_TO: "links to",
  RELATES_TO: "related to",
};

function humanEdgeLabel(t: string): string {
  return EDGE_TYPE_HUMAN[t.toUpperCase()] || t;
}

function SliceSummary({
  tab,
  counts,
  stats,
}: {
  tab: SliceTab;
  counts: ReturnType<typeof useMemo> extends infer T ? any : any;
  stats: GraphStats | null;
}) {
  const entityTypeRows = Object.entries(counts.entityByType as Record<string, number>)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1]);
  return (
    <div>
      <h3 className="font-display text-sm font-semibold text-primary mb-1 flex items-center gap-2">
        <Layers size={14} />
        Current slice
      </h3>
      <p className="text-[11px] text-text-muted mb-3 leading-snug">
        {tab.caption}
      </p>

      <div className="p-3 rounded-lg bg-surface-alt mb-3">
        <p className="text-xs text-text-main leading-relaxed">
          <span className="font-mono font-bold text-primary">
            {counts.pages.length}
          </span>{" "}
          page →{" "}
          <span className="font-mono font-bold text-primary">
            {counts.blocks.length}
          </span>{" "}
          snippets →{" "}
          <span className="font-mono font-bold text-primary">
            {counts.entities.length}
          </span>{" "}
          entities
          {counts.topics.length > 0 && (
            <>
              {" "}
              (+{" "}
              <span className="font-mono font-bold text-primary">
                {counts.topics.length}
              </span>{" "}
              topics)
            </>
          )}
        </p>
        <p className="text-[11px] text-text-muted mt-2">
          <span className="font-mono font-medium text-text-main">
            {counts.cross}
          </span>{" "}
          cross-layer edges,{" "}
          <span className="font-mono font-medium text-text-main">
            {counts.same}
          </span>{" "}
          same-layer edges.
        </p>
      </div>

      {entityTypeRows.length > 0 && (
        <div className="mb-3">
          <p className="text-[10px] text-text-muted font-mono mb-2">
            ENTITY BREAKDOWN
          </p>
          <div className="space-y-1">
            {entityTypeRows.map(([et, n]) => (
              <div
                key={et}
                className="flex items-center justify-between py-1 border-b border-border/50 last:border-0"
              >
                <span className="text-[11px] text-text-main">
                  {humanEntityTypeLabel(et)}
                </span>
                <span className="text-[11px] font-mono text-text-muted">
                  {n}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="rounded-lg bg-surface-alt p-3 text-[11px] text-text-muted leading-relaxed">
        <p className="font-mono text-[10px] mb-1.5 text-text-main">
          HOW TO READ THIS
        </p>
        <ul className="space-y-1 list-disc list-inside">
          <li>Hexagon at the center = the page that anchors this slice.</li>
          <li>Inner ring (grey squares) = DOM snippets from that page.</li>
          <li>Middle ring (green ellipses) = entities extracted from those snippets.</li>
          <li>Outer ring (small diamonds) = topic keywords each entity is tagged with.</li>
          <li>Radial spokes go between rings. Faint arcs stay within a ring.</li>
          <li>Click any node to dim everything not on its provenance chain.</li>
        </ul>
      </div>

      {stats && (
        <details className="mt-4 group">
          <summary className="cursor-pointer text-[10px] font-mono text-text-muted hover:text-text-main flex items-center gap-1">
            <ChevronRight
              size={10}
              className="group-open:rotate-90 transition-transform"
            />
            Full graph stats
          </summary>
          <FullGraphStats stats={stats} />
        </details>
      )}
    </div>
  );
}

function FreeExploreStats({ stats }: { stats: GraphStats | null }) {
  return (
    <>
      <h3 className="font-display text-sm font-semibold text-primary mb-4 flex items-center gap-2">
        <Info size={14} />
        Graph Statistics
      </h3>
      {stats && <FullGraphStats stats={stats} />}
      <div className="border-t border-border pt-3 mt-3 text-[11px] text-text-muted leading-relaxed">
        <p className="font-mono text-[10px] mb-1.5">TIPS</p>
        <ul className="space-y-1 list-disc list-inside">
          <li>Click a node to focus its neighbourhood</li>
          <li>Double-click to expand 1-hop neighbours</li>
          <li>Toggle legend items to filter types</li>
          <li>Use the branch icon to find shortest paths</li>
        </ul>
      </div>
    </>
  );
}

function FullGraphStats({ stats }: { stats: GraphStats }) {
  return (
    <div className="space-y-3 mt-2">
      {[
        {
          label: "Pages",
          value: stats.webpages,
          detail: `${stats.fetched_webpages ?? 0} fetched · ${stats.stub_webpages ?? 0} stubs`,
          color: NODE_TYPE_COLORS.WebPage,
        },
        { label: "Snippets", value: stats.blocks, color: NODE_TYPE_COLORS.Block },
        { label: "Entities", value: stats.entities, color: NODE_TYPE_COLORS.Entity },
        { label: "Topics", value: stats.topic_keywords, color: NODE_TYPE_COLORS.TopicKeyword },
      ].map((item) => (
        <div
          key={item.label}
          className="flex items-center justify-between p-2.5 rounded-lg bg-surface-alt"
        >
          <div className="flex items-center gap-2">
            <span
              className="w-2.5 h-2.5 rounded-sm"
              style={{ background: item.color }}
            />
            <span className="text-xs text-text-main">{item.label}</span>
          </div>
          <div className="text-right">
            <span className="block text-sm font-mono font-bold text-primary">
              {item.value.toLocaleString()}
            </span>
            {item.detail && (
              <span className="block text-[9px] text-text-muted mt-0.5">
                {item.detail}
              </span>
            )}
          </div>
        </div>
      ))}

      <div className="border-t border-border pt-3 mt-3">
        <p className="text-[10px] text-text-muted font-mono mb-2">EDGES</p>
        {[
          { label: "links to", value: stats.links_to },
          { label: "contains", value: stats.contains },
          { label: "related to", value: stats.relates_to },
          { label: "mentioned in", value: stats.extracted_from },
        ].map((item) => (
          <div
            key={item.label}
            className="flex items-center justify-between py-1"
          >
            <span className="text-xs text-text-muted">{item.label}</span>
            <span className="text-xs font-mono text-text-main">
              {item.value.toLocaleString()}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

const ENTITY_TYPE_HUMAN: Record<string, string> = {
  PERSON: "People",
  PROGRAMME: "Programmes",
  COURSE: "Courses",
  DEPARTMENT: "Departments",
  RESEARCH_AREA: "Research areas",
  FACILITY: "Facilities",
  POLICY: "Policies",
  SCHOLARSHIP: "Scholarships",
  EVENT: "Events",
  OTHER: "Other entities",
};

function humanEntityTypeLabel(et: string): string {
  const k = et.toUpperCase();
  if (ENTITY_TYPE_HUMAN[k]) return ENTITY_TYPE_HUMAN[k];
  return et.charAt(0).toUpperCase() + et.slice(1).toLowerCase();
}

function ToolButton({
  children,
  label,
  onClick,
  active,
}: {
  children: React.ReactNode;
  label: string;
  onClick: () => void;
  active?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      title={label}
      aria-label={label}
      className={`p-2 rounded-lg transition-colors ${
        active
          ? "bg-primary text-text-inverse"
          : "text-text-muted hover:bg-surface-alt hover:text-text-main"
      }`}
    >
      {children}
    </button>
  );
}

function DetailsPanel({
  title,
  typeLabel,
  accentColor,
  primary,
  subtitle,
  properties,
  extra,
  onClose,
}: {
  title: string;
  typeLabel: string;
  accentColor?: string;
  primary: string;
  subtitle: string;
  properties?: Record<string, unknown>;
  extra?: React.ReactNode;
  onClose: () => void;
}) {
  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-display text-sm font-semibold text-primary flex items-center gap-2">
          <span
            className="w-2.5 h-2.5 rounded-sm"
            style={{ background: accentColor }}
          />
          {title}
        </h3>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-surface-alt text-text-muted"
          aria-label="Close details"
        >
          <X size={14} />
        </button>
      </div>
      <div className="p-3 rounded-lg bg-surface-alt">
        <p className="text-[10px] font-mono text-text-muted uppercase mb-1">
          {typeLabel}
        </p>
        <p className="text-sm font-medium text-text-main break-words">
          {primary}
        </p>
        <p className="text-[11px] text-text-muted mt-1 break-all font-mono">
          {subtitle}
        </p>
      </div>

      {properties && Object.keys(properties).length > 0 && (
        <div className="mt-4">
          <p className="text-[10px] font-mono text-text-muted mb-2">
            PROPERTIES
          </p>
          <div className="space-y-1">
            {Object.entries(properties).map(([k, v]) => (
              <PropertyRow key={k} k={k} v={v} />
            ))}
          </div>
        </div>
      )}

      {extra}
    </div>
  );
}

function PropertyRow({ k, v }: { k: string; v: unknown }) {
  const [open, setOpen] = useState(false);
  const complex = v !== null && typeof v === "object";

  if (!complex) {
    return (
      <div className="flex items-start justify-between gap-2 py-1 border-b border-border/50 last:border-0">
        <span className="text-[11px] font-mono text-text-muted flex-none">
          {k}
        </span>
        <span className="text-[11px] text-text-main text-right break-all">
          {v === null || v === undefined ? (
            <span className="text-text-muted italic">null</span>
          ) : typeof v === "boolean" ? (
            String(v)
          ) : (
            String(v)
          )}
        </span>
      </div>
    );
  }

  return (
    <div className="py-1 border-b border-border/50 last:border-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between gap-2 text-left"
      >
        <span className="text-[11px] font-mono text-text-muted flex items-center gap-1">
          {open ? (
            <ChevronDown size={10} />
          ) : (
            <ChevronRight size={10} />
          )}
          {k}
        </span>
        <span className="text-[10px] text-text-muted">
          {Array.isArray(v) ? `[${v.length}]` : `{${Object.keys(v as object).length}}`}
        </span>
      </button>
      {open && (
        <pre className="mt-1 p-2 rounded bg-surface border border-border text-[10px] text-text-main overflow-x-auto whitespace-pre-wrap break-all">
          {JSON.stringify(v, null, 2)}
        </pre>
      )}
    </div>
  );
}
