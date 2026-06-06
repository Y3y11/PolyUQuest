"use client";

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import type { GraphData, GraphNode, GraphEdge } from "@/lib/api";

// Read CSS custom properties so node colors track theme switches.
// Cytoscape's color parser does not understand the modern space-separated
// `rgb(R G B)` syntax, so we emit comma-separated `rgb(R, G, B)`.
function tokenColor(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  if (!raw) return fallback;
  const parts = raw.split(/\s+/).filter(Boolean);
  if (parts.length < 3) return fallback;
  return `rgb(${parts[0]}, ${parts[1]}, ${parts[2]})`;
}

function buildNodeStyles(): Record<string, { shape: string; color: string }> {
  return {
    WebPage: {
      shape: "hexagon",
      color: tokenColor("--node-webpage", "#3B82F6"),
    },
    Entity: {
      shape: "ellipse",
      color: tokenColor("--node-entity", "#10B981"),
    },
    Block: {
      shape: "rectangle",
      color: tokenColor("--node-block", "#6B7280"),
    },
    TopicKeyword: {
      shape: "diamond",
      color: tokenColor("--node-topic", "#D4A853"),
    },
  };
}

let colaRegistered = false;
let svgRegistered = false;

// Trigger a browser download for an in-memory blob.
function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export interface GraphCanvasHandle {
  zoomIn: () => void;
  zoomOut: () => void;
  fit: () => void;
  runLayout: () => void;
  // Export the current canvas as a vector SVG (preferred) or, if the SVG
  // extension is unavailable at runtime, a high-resolution PNG.
  exportImage: () => Promise<void>;
  getCy: () => any;
}

export interface PathHighlight {
  nodes: Set<string>;
  edges: Set<string>; // key: `${source}__${target}`
}

export interface GraphCanvasProps {
  data: GraphData | null;
  visibleTypes?: Set<string>;
  focusedNodeId?: string | null;
  pathModeActive?: boolean;
  highlightedPath?: PathHighlight | null;
  layout?: "force" | "layered";
  showLabels?: boolean;
  onNodeClick?: (node: GraphNode) => void;
  onNodeDoubleClick?: (node: GraphNode) => void;
  onEdgeClick?: (edge: GraphEdge) => void;
  onBackgroundClick?: () => void;
  onNodeHover?: (
    node: GraphNode | null,
    position?: { x: number; y: number }
  ) => void;
}

// ---- On-canvas node label formatting -------------------------------------
// Raw labels from the API are noisy for a graph picture: page titles carry a
// long "| The Hong Kong Polytechnic University" suffix, Block labels are
// "A > B > C" heading-context breadcrumbs, and a flat char cut breaks words
// mid-token. We clean + word-boundary-truncate per node type instead.

const SITE_SUFFIX =
  /\s*[|–—\-:]\s*(the\s+)?hong kong polytechnic university\b.*$/i;

function cleanLabelText(raw: string): string {
  const collapsed = raw.replace(/\s+/g, " ").trim();
  const stripped = collapsed.replace(SITE_SUFFIX, "").trim();
  return stripped || collapsed; // never return empty if the title *was* the suffix
}

// Cut near `max` chars but prefer the last word boundary, so labels read as
// "Department of Computing…" rather than "Department of Computin…".
function truncateWords(s: string, max: number): string {
  if (s.length <= max) return s;
  const slice = s.slice(0, max);
  const lastSpace = slice.lastIndexOf(" ");
  const head = lastSpace > max * 0.6 ? slice.slice(0, lastSpace) : slice;
  return head.replace(/[\s.,;:|–—\-]+$/, "") + "…";
}

const LABEL_MAX: Record<string, number> = {
  WebPage: 30,
  Entity: 26,
  Block: 26,
  TopicKeyword: 18,
};

function formatNodeLabel(node: GraphNode): string {
  let raw = node.label || node.id || "";
  // Keep the most specific trailing segment of a heading-context breadcrumb —
  // it identifies the snippet far better than the generic site root.
  if (node.type === "Block" && raw.includes(">")) {
    const parts = raw
      .split(">")
      .map((p) => p.trim())
      .filter(Boolean);
    if (parts.length) raw = parts[parts.length - 1];
  }
  return truncateWords(cleanLabelText(raw), LABEL_MAX[node.type] ?? 24);
}

function edgeKey(src: string, tgt: string) {
  return `${src}__${tgt}`;
}

// Cross-layer edges form the WebPage → Block → Entity → Topic backbone the
// page is meant to make visible. Same-layer edges (LINKS_TO, RELATES_TO) are
// secondary and rendered dimmed in layered mode.
const CROSS_LAYER_EDGE_TYPES = new Set([
  "CONTAINS",
  "EXTRACTED_FROM",
  "HAS_TOPIC",
]);

// Radius per layer in concentric mode. Page sits at the center; blocks form
// the inner ring; entities form the middle ring; topics form the outer ring.
// These are *base* radii — the actual radius scales up with node count so
// nodes don't crowd each other at high counts (e.g. a 56-block department
// slice gets a much larger block ring than the 14-block programme slice).
const LAYER_BASE_RADIUS: Record<string, number> = {
  page: 0,
  block: 130,
  entity: 240,
  topic: 340,
};

// Minimum chord length (px) between adjacent nodes on the same ring — used
// to grow a ring's radius when it would otherwise be too crowded. ~60px
// gives a clean separation at the new (smaller) node sizes.
const MIN_NODE_SPACING = 60;

function layerOf(node: GraphNode): string {
  const fromProps = node.properties?.layer;
  if (
    typeof fromProps === "string" &&
    fromProps in LAYER_BASE_RADIUS
  )
    return fromProps;
  // fall back to type so non-slice data still has a sensible layer
  switch (node.type) {
    case "WebPage":
      return "page";
    case "Block":
      return "block";
    case "TopicKeyword":
      return "topic";
    default:
      return "entity";
  }
}

// Compute concentric (x, y) positions for a layered slice:
//   - page    →  center (0, 0). Multiple pages share the center cluster.
//   - blocks  →  inner ring, ordered by depth then id (deterministic).
//   - entities → middle ring, angularly placed by the mean angle of their
//                connected blocks (so cross-layer EXTRACTED_FROM edges read
//                as short near-radial lines, not chords across the disc).
//   - topics  → outer ring, placed near the angle of their parent entity
//                so HAS_TOPIC edges also stay near-radial.
//
// Same-layer edges (LINKS_TO, RELATES_TO) ride along the ring as short arcs
// rather than chords through the center.
function computeLayeredPositions(
  data: GraphData
): Map<string, { x: number; y: number }> {
  const pos = new Map<string, { x: number; y: number }>();

  const byLayer: Record<string, GraphNode[]> = {
    page: [],
    block: [],
    entity: [],
    topic: [],
  };
  for (const n of data.nodes) {
    const lyr = layerOf(n);
    (byLayer[lyr] ||= []).push(n);
  }

  // Grow a ring's radius until each adjacent pair of nodes has at least
  // MIN_NODE_SPACING px of chord between them. The base radius is the floor;
  // we never shrink it. `innerRadius` is the radius of the previous ring —
  // each ring must always sit OUTSIDE the previous one, otherwise a
  // high-node-count inner ring (e.g. 56 blocks) would push entities and
  // topics inside it.
  const RING_GAP = 90; // minimum radial spacing between consecutive rings
  const ringRadius = (
    count: number,
    baseRadius: number,
    innerRadius = 0
  ): number => {
    let r = baseRadius;
    if (count > 1) {
      const needed = (count * MIN_NODE_SPACING) / (2 * Math.PI);
      r = Math.max(r, needed);
    }
    return Math.max(r, innerRadius + RING_GAP);
  };

  // Place `nodes` on a ring of radius `r`, starting at `startAngle`, evenly
  // spaced clockwise. Returns the assigned angle per node id.
  const placeOnRing = (
    nodes: GraphNode[],
    r: number,
    startAngle = -Math.PI / 2 // start at the top of the circle
  ): Map<string, number> => {
    const angles = new Map<string, number>();
    const n = nodes.length;
    if (n === 0) return angles;
    if (n === 1) {
      const a = startAngle;
      angles.set(nodes[0].id, a);
      pos.set(nodes[0].id, {
        x: r * Math.cos(a),
        y: r * Math.sin(a),
      });
      return angles;
    }
    const step = (2 * Math.PI) / n;
    nodes.forEach((node, i) => {
      const a = startAngle + i * step;
      angles.set(node.id, a);
      pos.set(node.id, {
        x: r * Math.cos(a),
        y: r * Math.sin(a),
      });
    });
    return angles;
  };

  // Page layer — center anchor (or tight cluster if multiple)
  if (byLayer.page.length === 1) {
    pos.set(byLayer.page[0].id, { x: 0, y: 0 });
  } else {
    placeOnRing(byLayer.page, 30);
  }

  // Block layer — inner ring, sorted by depth then id for determinism
  const blocks = [...byLayer.block].sort((a, b) => {
    const ad = Number(a.properties?.depth ?? 0);
    const bd = Number(b.properties?.depth ?? 0);
    if (ad !== bd) return ad - bd;
    return a.id.localeCompare(b.id);
  });
  const blockRadius = ringRadius(blocks.length, LAYER_BASE_RADIUS.block);
  const blockAngles = placeOnRing(blocks, blockRadius);

  // Build edge index by type for angular barycenter computation
  const extractedFrom: Array<[string, string]> = []; // [entityId, blockId]
  const hasTopic: Array<[string, string]> = []; // [entityId, topicId]
  for (const e of data.edges) {
    const t = (e.type || "").toUpperCase();
    if (t === "EXTRACTED_FROM") extractedFrom.push([e.source, e.target]);
    else if (t === "HAS_TOPIC") hasTopic.push([e.source, e.target]);
  }

  const entityToBlocks = new Map<string, string[]>();
  for (const [eid, bid] of extractedFrom) {
    if (!entityToBlocks.has(eid)) entityToBlocks.set(eid, []);
    entityToBlocks.get(eid)!.push(bid);
  }

  // Angular mean using sin/cos vector average — handles the 0/2π wraparound
  // correctly (a node connected to angles 350° and 10° gets mean 0°, not 180°).
  const angularMean = (angles: number[]): number => {
    if (angles.length === 0) return -Math.PI / 2;
    let sx = 0,
      sy = 0;
    for (const a of angles) {
      sx += Math.cos(a);
      sy += Math.sin(a);
    }
    return Math.atan2(sy, sx);
  };

  // Entity layer — middle ring, angle = mean angle of connected blocks
  const entityBary = byLayer.entity.map((ent) => {
    const blockIds = entityToBlocks.get(ent.id) || [];
    const blockAnglesForEnt = blockIds
      .map((id) => blockAngles.get(id))
      .filter((a): a is number => typeof a === "number");
    const angle = blockAnglesForEnt.length
      ? angularMean(blockAnglesForEnt)
      : -Math.PI / 2;
    return { ent, angle };
  });
  // Sort by barycenter angle, then redistribute so entities stay evenly
  // spaced on the ring (preserves the rough angular order from blocks)
  entityBary.sort(
    (a, b) => a.angle - b.angle || a.ent.id.localeCompare(b.ent.id)
  );
  const entityRadius = ringRadius(
    entityBary.length,
    LAYER_BASE_RADIUS.entity,
    blockRadius
  );
  const entityAngles = new Map<string, number>();
  const entN = entityBary.length;
  if (entN > 0) {
    const step = (2 * Math.PI) / entN;
    // anchor the redistributed ring at the first entity's barycenter to keep
    // visual continuity with the block ring
    const anchor = entityBary[0].angle;
    entityBary.forEach((eb, i) => {
      const a = anchor + i * step;
      entityAngles.set(eb.ent.id, a);
      pos.set(eb.ent.id, {
        x: entityRadius * Math.cos(a),
        y: entityRadius * Math.sin(a),
      });
    });
  }

  // Topic layer — outer ring, angle = mean angle of parent entities (so
  // topics sit near their entity along the same radial line). Topics with
  // multiple parents take the angular mean of those parents' angles.
  const entityToTopics = new Map<string, string[]>();
  const topicToEntities = new Map<string, string[]>();
  for (const [eid, tid] of hasTopic) {
    if (!entityToTopics.has(eid)) entityToTopics.set(eid, []);
    entityToTopics.get(eid)!.push(tid);
    if (!topicToEntities.has(tid)) topicToEntities.set(tid, []);
    topicToEntities.get(tid)!.push(eid);
  }

  const topicRadius = ringRadius(
    byLayer.topic.length,
    LAYER_BASE_RADIUS.topic,
    entityRadius
  );
  // First assign each topic a desired angle from its parent entities
  const topicDesiredAngles = byLayer.topic.map((t) => {
    const parents = topicToEntities.get(t.id) || [];
    const parentAngles = parents
      .map((eid) => entityAngles.get(eid))
      .filter((a): a is number => typeof a === "number");
    const angle = parentAngles.length
      ? angularMean(parentAngles)
      : -Math.PI / 2;
    return { t, angle };
  });
  // Sort by desired angle and redistribute evenly to prevent overlap on the
  // ring, but preserve the ordering so each topic stays near its parent.
  topicDesiredAngles.sort(
    (a, b) => a.angle - b.angle || a.t.id.localeCompare(b.t.id)
  );
  if (topicDesiredAngles.length > 0) {
    const step = (2 * Math.PI) / topicDesiredAngles.length;
    const anchor = topicDesiredAngles[0].angle;
    topicDesiredAngles.forEach((td, i) => {
      const a = anchor + i * step;
      pos.set(td.t.id, {
        x: topicRadius * Math.cos(a),
        y: topicRadius * Math.sin(a),
      });
    });
  }

  return pos;
}

const ENTITY_TYPE_LABEL: Record<string, string> = {
  PERSON: "Person",
  PROGRAMME: "Programme",
  COURSE: "Course",
  DEPARTMENT: "Department",
  RESEARCH_AREA: "Research area",
  FACILITY: "Facility",
  POLICY: "Policy",
  SCHOLARSHIP: "Scholarship",
  EVENT: "Event",
};

// Human-readable type label shown in the legend / tooltip. WebPage → Page,
// Block → Snippet, Entity → use its `entity_type` if known, TopicKeyword → Topic.
export function humanNodeTypeLabel(node: GraphNode): string {
  if (node.type === "Entity") {
    const et = node.properties?.entity_type;
    if (typeof et === "string" && et) {
      return (
        ENTITY_TYPE_LABEL[et.toUpperCase()] ||
        et.charAt(0).toUpperCase() + et.slice(1).toLowerCase()
      );
    }
    return "Entity";
  }
  if (node.type === "WebPage") return "Page";
  if (node.type === "Block") return "Snippet";
  if (node.type === "TopicKeyword") return "Topic";
  return node.type;
}

// Apply the concentric three-layer positions. We use `preset` here so the
// computed (x, y) coordinates stay exact — concentric must remain concentric.
// (Running cose on top would relax the rings back into a blob.)
function applyLayeredPositions(cy: any, data: GraphData) {
  const pos = computeLayeredPositions(data);
  cy.batch(() => {
    cy.nodes().forEach((n: any) => {
      const p = pos.get(n.id());
      if (p) n.position(p);
    });
  });
  // Tab swaps showed the new ring landing offscreen because `preset`'s
  // `fit: true` was being evaluated against the previous slice's viewport
  // bbox. Fit explicitly on the next frame (positions are already committed
  // by then) and animate the camera over the same window the rings would
  // have animated, so it still feels like a single transition.
  requestAnimationFrame(() => {
    cy.animate(
      { fit: { eles: cy.elements(), padding: 60 } },
      { duration: 600, easing: "ease-in-out" }
    );
  });
}

const GraphCanvas = forwardRef<GraphCanvasHandle, GraphCanvasProps>(
  function GraphCanvas(
    {
      data,
      visibleTypes,
      focusedNodeId,
      pathModeActive,
      highlightedPath,
      layout = "force",
      showLabels = true,
      onNodeClick,
      onNodeDoubleClick,
      onEdgeClick,
      onBackgroundClick,
      onNodeHover,
    },
    ref
  ) {
    const containerRef = useRef<HTMLDivElement>(null);
    const cyRef = useRef<any>(null);
    const nodeLookupRef = useRef<Map<string, GraphNode>>(new Map());
    const edgeLookupRef = useRef<Map<string, GraphEdge>>(new Map());
    const lastTapRef = useRef<{ id: string; t: number } | null>(null);
    const layoutRef = useRef(layout);
    layoutRef.current = layout;
    const dataRef = useRef<GraphData | null>(null);
    dataRef.current = data;
    const [ready, setReady] = useState(false);

    // Keep latest handler refs so we don't have to rebind Cytoscape events
    const handlersRef = useRef({
      onNodeClick,
      onNodeDoubleClick,
      onEdgeClick,
      onBackgroundClick,
      onNodeHover,
    });
    handlersRef.current = {
      onNodeClick,
      onNodeDoubleClick,
      onEdgeClick,
      onBackgroundClick,
      onNodeHover,
    };

    useImperativeHandle(
      ref,
      () => ({
        zoomIn: () => {
          const cy = cyRef.current;
          if (!cy) return;
          cy.animate({ zoom: cy.zoom() * 1.25 }, { duration: 150 });
        },
        zoomOut: () => {
          const cy = cyRef.current;
          if (!cy) return;
          cy.animate({ zoom: cy.zoom() / 1.25 }, { duration: 150 });
        },
        fit: () => {
          cyRef.current?.fit(undefined, 50);
        },
        runLayout: () => {
          const cy = cyRef.current;
          if (!cy) return;
          if (layoutRef.current === "layered" && dataRef.current) {
            applyLayeredPositions(cy, dataRef.current);
            return;
          }
          cy.layout({
            name: colaRegistered ? "cola" : "cose",
            animate: true,
            animationDuration: 600,
            nodeSpacing: 40,
          } as any).run();
        },
        exportImage: async () => {
          const cy = cyRef.current;
          if (!cy) return;
          // Match the export background to the current theme's surface (white
          // in the "paper" capture theme) so the figure drops into the paper
          // without an off-colour rectangle. `full: true` exports the whole
          // graph at its modelled extent, not just the visible viewport.
          const bg = tokenColor("--color-surface", "#ffffff");
          const stamp = new Date()
            .toISOString()
            .slice(0, 19)
            .replace(/[:T]/g, "-");
          if (svgRegistered && typeof cy.svg === "function") {
            try {
              const svg: string = cy.svg({ full: true, bg, scale: 1 });
              downloadBlob(
                new Blob([svg], { type: "image/svg+xml;charset=utf-8" }),
                `polyuquest-graph-${stamp}.svg`
              );
              return;
            } catch (err) {
              console.error("SVG export failed, falling back to PNG", err);
            }
          }
          const blob: Blob = cy.png({
            full: true,
            bg,
            scale: 3,
            output: "blob",
          });
          downloadBlob(blob, `polyuquest-graph-${stamp}.png`);
        },
        getCy: () => cyRef.current,
      }),
      []
    );

    // Initialise Cytoscape once — subsequent data updates use incremental merge.
    useEffect(() => {
      let cancelled = false;

      const init = async () => {
        if (!containerRef.current) return;
        const cytoscape = (await import("cytoscape")).default;
        if (!colaRegistered) {
          try {
            const cola = (await import("cytoscape-cola")).default;
            cytoscape.use(cola);
            colaRegistered = true;
          } catch {
            // cola not available, fall back to cose
          }
        }
        if (!svgRegistered) {
          try {
            const svg = (await import("cytoscape-svg")).default;
            cytoscape.use(svg);
            svgRegistered = true;
          } catch {
            // svg extension unavailable — export falls back to PNG
          }
        }
        if (cancelled || !containerRef.current) return;

        const cy = cytoscape({
          container: containerRef.current,
          elements: [],
          style: [
            {
              selector: "node",
              style: {
                label: "data(label)",
                "text-valign": "bottom" as any,
                "text-halign": "center" as any,
                "text-margin-y": 5,
                "font-size": "10px",
                "font-weight": 500 as any,
                "font-family": "var(--font-body), sans-serif",
                // Cytoscape's canvas renderer does NOT resolve CSS custom
                // properties, so label/outline colours must be read out as
                // concrete rgb() values (the theme effect re-applies them on
                // theme switch). A surface-coloured outline gives every label a
                // halo so it stays legible over edges and neighbouring nodes —
                // and reads cleanly in exported figures.
                color: tokenColor("--color-text-main", "#2A2422"),
                "text-outline-color": tokenColor("--color-surface", "#FBF7F0"),
                "text-outline-width": 2.4 as any,
                "text-outline-opacity": 1 as any,
                "background-color": "data(color)",
                shape: "data(shape)" as any,
                width: 32,
                height: 32,
                "border-width": 2,
                "border-color": "var(--color-border)",
                "transition-property":
                  "opacity, border-width, border-color, width, height" as any,
                "transition-duration": 150 as any,
              },
            },
            // In concentric mode, size encodes layer: page is the largest
            // anchor at center, block and entity are mid-tier on the inner /
            // middle rings, topics are the smallest on the outer ring. Sizes
            // are tuned smaller than the cose blob view so the rings stay
            // readable without overlap.
            {
              selector: "node.in-layered",
              style: {
                "text-wrap": "wrap" as any,
                "text-max-width": "90px" as any,
              },
            },
            {
              selector: "node.in-layered.layer-page",
              style: {
                width: 32,
                height: 32,
                "font-size": "10px",
                "font-weight": 600 as any,
                "border-width": 2,
                "text-max-width": "140px" as any,
              },
            },
            {
              selector: "node.in-layered.layer-block",
              style: { width: 18, height: 18, "border-width": 1 },
            },
            {
              selector: "node.in-layered.layer-entity",
              style: { width: 22, height: 22, "border-width": 1 },
            },
            {
              selector: "node.in-layered.layer-topic",
              style: {
                width: 12,
                height: 12,
                "font-size": "9px",
                opacity: 0.85 as any,
                "border-width": 1,
                "text-max-width": "60px" as any,
              },
            },
            {
              // Hide labels globally when the user toggles "Show titles" off.
              // The node itself stays visible — we just blank the label so the
              // canvas reads as a pure shape/colour cluster.
              selector: "node.no-label",
              style: { label: "" },
            },
            {
              selector: "edge",
              style: {
                label: "",
                "font-size": "8px",
                color: "var(--color-text-muted)",
                "line-color": "var(--color-border)",
                "target-arrow-color": "var(--color-border)",
                "target-arrow-shape": "triangle" as any,
                "curve-style": "bezier" as any,
                width: 1.5,
                "transition-property": "opacity, line-color, width" as any,
                "transition-duration": 150 as any,
              },
            },
            {
              // Cross-layer edges in layered mode are the page→block→entity→topic
              // backbone — emphasized so the three-layer story reads at a glance.
              selector: "edge.cross-layer",
              style: {
                "line-color": "rgb(var(--node-entity))",
                "target-arrow-color": "rgb(var(--node-entity))",
                width: 2.2,
                opacity: 0.95 as any,
              },
            },
            {
              // Cross-layer edges in concentric mode become near-radial
              // spokes from the center outward. Use straight-line edges
              // (haystack) so the radial structure reads clearly; no arrow,
              // slim line, soft opacity.
              selector: "edge.cross-layer.in-layered",
              style: {
                "curve-style": "straight" as any,
                width: 1,
                "target-arrow-shape": "none" as any,
                opacity: 0.55 as any,
              },
            },
            {
              // Same-layer edges (LINKS_TO, RELATES_TO subtypes): visible but
              // visually quieter than the cross-layer backbone. Dashed so they
              // read as secondary connections without disappearing entirely.
              selector: "edge.same-layer",
              style: {
                "line-color": "var(--color-border-strong)",
                "target-arrow-color": "var(--color-border-strong)",
                width: 0.8,
                opacity: 0.4 as any,
                "line-style": "dashed" as any,
              },
            },
            {
              // In concentric mode, same-layer edges arc along their ring.
              // Use unbundled-bezier with a small control-point distance so
              // the arc follows the ring's curvature rather than cutting
              // across the disc.
              selector: "edge.same-layer.in-layered",
              style: {
                "curve-style": "unbundled-bezier" as any,
                "control-point-distances": [20] as any,
                "control-point-weights": [0.5] as any,
                opacity: 0.2 as any,
                width: 0.7,
                "target-arrow-shape": "none" as any,
              },
            },
            {
              selector: "edge.labelled",
              style: { label: "data(label)" },
            },
            {
              selector: "node:selected",
              style: {
                "border-color": "var(--color-accent)",
                "border-width": 3,
              },
            },
            {
              selector: ".faded",
              style: { opacity: 0.12, "text-opacity": 0.15 as any },
            },
            {
              selector: ".highlighted",
              style: {
                "border-color": "var(--color-accent)",
                "border-width": 4,
              },
            },
            {
              selector: "edge.highlighted",
              style: {
                "line-color": "var(--color-accent)",
                "target-arrow-color": "var(--color-accent)",
                width: 2.5,
              },
            },
            {
              selector: ".on-path",
              style: {
                "border-color": "rgb(var(--color-accent))",
                "border-width": 4,
              },
            },
            {
              selector: "edge.on-path",
              style: {
                "line-color": "rgb(var(--color-accent))",
                "target-arrow-color": "rgb(var(--color-accent))",
                width: 3,
                label: "data(label)",
              },
            },
            {
              selector: ".path-endpoint",
              style: {
                "border-color": "rgb(var(--color-primary))",
                "border-width": 5,
              },
            },
            {
              selector: ".hidden",
              style: { display: "none" as any },
            },
          ],
          layout: { name: "preset" } as any,
          wheelSensitivity: 0.2,
          minZoom: 0.1,
          maxZoom: 3,
        });

        // Events
        cy.on("tap", "node", (evt: any) => {
          const n = evt.target;
          const now = Date.now();
          const id = n.id();
          const last = lastTapRef.current;
          if (last && last.id === id && now - last.t < 320) {
            lastTapRef.current = null;
            const full = nodeLookupRef.current.get(id);
            if (full) handlersRef.current.onNodeDoubleClick?.(full);
            return;
          }
          lastTapRef.current = { id, t: now };
          const full = nodeLookupRef.current.get(id);
          if (full) handlersRef.current.onNodeClick?.(full);
        });

        cy.on("tap", "edge", (evt: any) => {
          const e = evt.target;
          const key = edgeKey(e.source().id(), e.target().id());
          const full = edgeLookupRef.current.get(key);
          if (full) handlersRef.current.onEdgeClick?.(full);
        });

        cy.on("tap", (evt: any) => {
          if (evt.target === cy) {
            handlersRef.current.onBackgroundClick?.();
          }
        });

        cy.on("mouseover", "node", (evt: any) => {
          const n = evt.target;
          const full = nodeLookupRef.current.get(n.id());
          const rp = n.renderedPosition();
          if (full) {
            handlersRef.current.onNodeHover?.(full, { x: rp.x, y: rp.y });
          }
          containerRef.current?.style.setProperty("cursor", "pointer");
        });

        cy.on("mouseout", "node", () => {
          handlersRef.current.onNodeHover?.(null);
          containerRef.current?.style.setProperty("cursor", "default");
        });

        cyRef.current = cy;
        setReady(true);
      };

      init();

      return () => {
        cancelled = true;
        if (cyRef.current) {
          cyRef.current.destroy();
          cyRef.current = null;
        }
        nodeLookupRef.current.clear();
        edgeLookupRef.current.clear();
      };
    }, []);

    // Recolor nodes when the colour theme changes. Node colours come from
    // `data(color)`, which is otherwise only computed during a data update —
    // so without this, switching to the "paper" capture theme would leave
    // nodes in the previous theme's palette until the next reload. We re-read
    // the --node-* tokens and rewrite each node's colour in place.
    useEffect(() => {
      if (!ready) return;
      const recolor = () => {
        const cy = cyRef.current;
        if (!cy) return;
        const nodeStyles = buildNodeStyles();
        const labelColor = tokenColor("--color-text-main", "#2A2422");
        const outlineColor = tokenColor("--color-surface", "#FBF7F0");
        cy.batch(() => {
          cy.nodes().forEach((n: any) => {
            const style = nodeStyles[n.data("nodeType")] || nodeStyles.Entity;
            n.data("color", style.color);
          });
          // Label + halo colours live in the stylesheet (resolved at init), so
          // re-apply them as an inline override when the theme changes.
          cy.nodes().style({
            color: labelColor,
            "text-outline-color": outlineColor,
          });
        });
      };
      const observer = new MutationObserver((muts) => {
        if (muts.some((m) => m.attributeName === "data-theme")) recolor();
      });
      observer.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme"],
      });
      return () => observer.disconnect();
    }, [ready]);

    // Sync data into cytoscape (incremental merge preserves existing layout).
    useEffect(() => {
      const cy = cyRef.current;
      if (!cy || !ready) return;

      // Treat an empty payload (e.g. a search that returned no nodes) the same
      // as no data — otherwise the previous graph stays on screen and the user
      // thinks the search silently failed.
      if (!data || data.nodes.length === 0) {
        cy.elements().remove();
        nodeLookupRef.current.clear();
        edgeLookupRef.current.clear();
        return;
      }

      const incomingNodeIds = new Set<string>();
      const incomingEdgeKeys = new Set<string>();
      const newNodeIds: string[] = [];

      const nodeStyles = buildNodeStyles();
      const isLayeredMode = layoutRef.current === "layered";
      // Add/update nodes
      for (const node of data.nodes) {
        if (!node.id) continue;
        incomingNodeIds.add(node.id);
        nodeLookupRef.current.set(node.id, node);
        const existing = cy.getElementById(node.id);
        const style = nodeStyles[node.type] || nodeStyles.Entity;
        const labelText = formatNodeLabel(node);
        const nodeLayer = layerOf(node);
        const nodeClasses = isLayeredMode
          ? `in-layered layer-${nodeLayer}`
          : `layer-${nodeLayer}`;
        if (existing && existing.length > 0) {
          existing.data({
            label: labelText,
            nodeType: node.type,
            layer: nodeLayer,
            ...style,
          });
          existing.removeClass("in-layered layer-page layer-block layer-entity layer-topic");
          existing.addClass(nodeClasses);
        } else {
          cy.add({
            group: "nodes",
            data: {
              id: node.id,
              label: labelText,
              nodeType: node.type,
              layer: nodeLayer,
              ...style,
            },
            classes: nodeClasses,
          });
          newNodeIds.push(node.id);
        }
      }

      // Add/update edges
      for (const edge of data.edges) {
        if (!edge.source || !edge.target) continue;
        if (
          !incomingNodeIds.has(edge.source) &&
          cy.getElementById(edge.source).length === 0
        )
          continue;
        if (
          !incomingNodeIds.has(edge.target) &&
          cy.getElementById(edge.target).length === 0
        )
          continue;
        const key = edgeKey(edge.source, edge.target);
        incomingEdgeKeys.add(key);
        edgeLookupRef.current.set(key, edge);
        const isCrossLayer = CROSS_LAYER_EDGE_TYPES.has(
          (edge.type || "").toUpperCase()
        );
        const baseClass = isCrossLayer ? "cross-layer" : "same-layer";
        const classes = isLayeredMode ? `${baseClass} in-layered` : baseClass;
        const existing = cy.$(`edge[id = "${key}"]`);
        if (existing.length === 0) {
          cy.add({
            group: "edges",
            data: {
              id: key,
              source: edge.source,
              target: edge.target,
              label: edge.type || "",
              edgeType: edge.type || "",
            },
            classes,
          });
        } else {
          existing.data({ label: edge.type || "", edgeType: edge.type || "" });
          existing.removeClass("cross-layer same-layer in-layered");
          existing.addClass(classes);
        }
      }

      // Remove elements no longer present (but only when doing a full reset —
      // we detect that by checking if every existing id is in the incoming set;
      // neighbour-expansion merges will have a strict superset).
      const existingNodeIds = cy.nodes().map((n: any) => n.id());
      const allExistingInIncoming = existingNodeIds.every((id: string) =>
        incomingNodeIds.has(id)
      );
      const overlap = existingNodeIds.filter((id: string) =>
        incomingNodeIds.has(id)
      ).length;
      // Treat as a full reset when: (a) the incoming set is meaningfully
      // smaller than existing OR (b) there's zero overlap (tab/slice swap).
      const isFullReset =
        !allExistingInIncoming &&
        incomingNodeIds.size > 0 &&
        ((existingNodeIds.length > 0 && overlap === 0) ||
          incomingNodeIds.size < existingNodeIds.length * 0.8);
      if (isFullReset) {
        cy.elements().forEach((el: any) => {
          if (el.isNode() && !incomingNodeIds.has(el.id())) el.remove();
          else if (
            el.isEdge() &&
            !incomingEdgeKeys.has(edgeKey(el.source().id(), el.target().id()))
          )
            el.remove();
        });
      }

      // Layout: fit-all for first render or full reset (e.g. tab swap);
      // only-new layout for incremental expansions.
      if (newNodeIds.length > 0) {
        const firstRender = existingNodeIds.length === 0 || isFullReset;
        const isLayered = layoutRef.current === "layered";
        if (firstRender) {
          if (isLayered) {
            applyLayeredPositions(cy, data);
          } else {
            cy.layout({
              name: colaRegistered ? "cola" : "cose",
              animate: true,
              animationDuration: 600,
              nodeSpacing: 40,
              fit: true,
            } as any).run();
          }
        } else {
          // place new nodes near the first existing neighbour, then relax only them
          const existing = cy.nodes().filter((n: any) => !newNodeIds.includes(n.id()));
          newNodeIds.forEach((id) => {
            const node = cy.getElementById(id);
            const neighbours = node
              .connectedEdges()
              .connectedNodes()
              .filter((n: any) => !newNodeIds.includes(n.id()));
            const anchor = neighbours.length > 0 ? neighbours[0] : existing[0];
            if (anchor) {
              const p = anchor.position();
              node.position({
                x: p.x + (Math.random() - 0.5) * 80,
                y: p.y + (Math.random() - 0.5) * 80,
              });
            }
          });
          const newColl = cy.collection(
            newNodeIds.map((id) => cy.getElementById(id))
          );
          newColl
            .union(newColl.connectedEdges())
            .layout({
              name: colaRegistered ? "cola" : "cose",
              animate: true,
              animationDuration: 400,
              nodeSpacing: 40,
              fit: false,
              randomize: false,
            } as any)
            .run();
        }
      }
    }, [data, ready]);

    // React to layout mode changes mid-session — re-stamp the `in-layered`
    // classes on all existing elements and re-run the appropriate layout.
    useEffect(() => {
      const cy = cyRef.current;
      if (!cy || !ready) return;
      cy.batch(() => {
        cy.nodes().forEach((n: any) => {
          if (layout === "layered") n.addClass("in-layered");
          else n.removeClass("in-layered");
        });
        cy.edges().forEach((e: any) => {
          if (layout === "layered") e.addClass("in-layered");
          else e.removeClass("in-layered");
        });
      });
      if (layout === "layered" && dataRef.current && cy.nodes().length > 0) {
        applyLayeredPositions(cy, dataRef.current);
      } else if (layout === "force" && cy.nodes().length > 0) {
        cy.layout({
          name: colaRegistered ? "cola" : "cose",
          animate: true,
          animationDuration: 500,
          nodeSpacing: 40,
          fit: true,
        } as any).run();
      }
    }, [layout, ready]);

    // Toggle node titles on/off via a class. The `node.no-label` style rule
    // blanks the label so the canvas shows pure shape + colour clusters when
    // the user wants a less noisy view.
    useEffect(() => {
      const cy = cyRef.current;
      if (!cy || !ready) return;
      cy.batch(() => {
        cy.nodes().forEach((n: any) => {
          n.toggleClass("no-label", !showLabels);
        });
      });
    }, [showLabels, ready, data]);

    // Apply type-visibility filter
    useEffect(() => {
      const cy = cyRef.current;
      if (!cy || !ready) return;
      cy.batch(() => {
        cy.nodes().forEach((n: any) => {
          const t = n.data("nodeType");
          const hidden = visibleTypes && !visibleTypes.has(t);
          n.toggleClass("hidden", !!hidden);
        });
        cy.edges().forEach((e: any) => {
          const src = e.source();
          const tgt = e.target();
          const hidden =
            src.hasClass("hidden") || tgt.hasClass("hidden");
          e.toggleClass("hidden", hidden);
        });
      });
    }, [visibleTypes, ready, data]);

    // Apply neighborhood focus when a node is selected (and no path highlight active)
    useEffect(() => {
      const cy = cyRef.current;
      if (!cy || !ready) return;
      // Path highlight takes precedence — skip if active
      if (highlightedPath) return;
      cy.batch(() => {
        cy.elements().removeClass("faded highlighted");
        if (!focusedNodeId) return;
        const node = cy.getElementById(focusedNodeId);
        if (!node || node.length === 0) return;
        const neighbors = node.closedNeighborhood();
        cy.elements().not(neighbors).addClass("faded");
        neighbors.addClass("highlighted");
        node.removeClass("faded");
      });
    }, [focusedNodeId, highlightedPath, ready, data]);

    // Apply highlightedPath
    useEffect(() => {
      const cy = cyRef.current;
      if (!cy || !ready) return;
      cy.batch(() => {
        cy.elements().removeClass("on-path faded path-endpoint highlighted");
        if (!highlightedPath) return;
        const { nodes, edges } = highlightedPath;
        cy.elements().addClass("faded");
        cy.nodes().forEach((n: any) => {
          if (nodes.has(n.id())) {
            n.removeClass("faded");
            n.addClass("on-path");
          }
        });
        cy.edges().forEach((e: any) => {
          const k1 = edgeKey(e.source().id(), e.target().id());
          const k2 = edgeKey(e.target().id(), e.source().id());
          if (edges.has(k1) || edges.has(k2)) {
            e.removeClass("faded");
            e.addClass("on-path");
          }
        });
        // mark endpoints
        if (nodes.size >= 2) {
          const arr = Array.from(nodes);
          const first = cy.getElementById(arr[0]);
          const last = cy.getElementById(arr[arr.length - 1]);
          if (first) first.addClass("path-endpoint");
          if (last) last.addClass("path-endpoint");
        }
      });
    }, [highlightedPath, ready]);

    // Cursor feedback for path mode
    useEffect(() => {
      const el = containerRef.current;
      if (!el) return;
      el.style.cursor = pathModeActive ? "crosshair" : "default";
    }, [pathModeActive]);

    return (
      <div
        ref={containerRef}
        className="w-full h-full min-h-[500px] rounded-xl border border-border bg-surface-alt"
      />
    );
  }
);

export default GraphCanvas;
