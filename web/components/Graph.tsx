"use client";

import {
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  getSmoothStepPath,
  useNodesState,
  useReactFlow,
  type Edge,
  type EdgeProps,
  type FitViewOptions,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/base.css";
import { memo, useEffect, useLayoutEffect, useMemo, useRef } from "react";

import { EDGES, META, NODES, POSITIONS, edgeState, formatMs, serviceOf, stateWord, type EdgeState, type GraphView, type NodeMeta, type NodeView } from "@/lib/graph";
import { SOURCE_LABELS } from "@/lib/format";
import type { NodeId, NodeState } from "@/lib/types";
import { StateGlyph } from "./Icons";
import { useEntered } from "./Motion";

// The states a step finishes in. A step that reaches one on screen fills its mark and settles its badge.
const FINISHED: NodeState[] = ["done", "failed", "skipped", "waiting"];
const finished = (state: NodeState) => FINISHED.includes(state);

// Pixel padding, so the graph's scale depends only on the space it has: the 800-unit-wide layout is drawn at
// full size or close to it from a 1280 px screen up. The bottom keeps clear of the React Flow attribution.
const FIT: FitViewOptions = { padding: { top: "12px", right: "12px", bottom: "22px", left: "12px" }, duration: 0 };

export function SourceBadge({ view }: { view: NodeView }) {
  const source = view.source;
  if (!source || view.state === "running" || view.state === "queued" || view.state === "idle") return null;
  return <span className={`badge ${source}`}>{SOURCE_LABELS[source] ?? source}</span>;
}

/** One pipeline step: name, service, source and measured time. A button, so it is reachable by keyboard.
 * A finished step shows its check mark, badge and time; the word is written out for every other state. While it
 * runs the card pulses; when it finishes on screen (data-enter) its mark fills and draws in. */
export function LaneCard({ meta, view, selected, onOpen }: { meta: NodeMeta; view: NodeView; selected: boolean; onOpen: (id: NodeId) => void }) {
  const entered = useEntered(view.state, finished);
  const word = stateWord(view);
  const service = serviceOf(meta, view);
  const label = `${meta.name}: ${word}${view.duration_ms != null ? `, ${formatMs(view.duration_ms)}` : ""}${view.source ? `, ${SOURCE_LABELS[view.source] ?? view.source}` : ""}. ${view.summary}`;
  const plainDone = view.state === "done" && view.decision !== "turned_down";
  const timed = view.state === "done" || view.state === "failed";
  return (
    <button type="button" className="lane nodrag nopan" data-state={view.state} data-selected={selected} data-enter={entered ? view.state : undefined}
            aria-label={label} title={view.summary || service} onClick={() => onOpen(meta.id)}>
      <StateGlyph state={view.state} />
      <span className="name">{meta.name}</span>
      <span className="svc">{service}</span>
      <span className="meta">
        {plainDone ? null : <span className="state-word">{word}</span>}
        <SourceBadge view={view} />
        <span className="ms num">{timed ? formatMs(view.duration_ms) : ""}</span>
      </span>
    </button>
  );
}

type LaneData = { meta: NodeMeta; view: NodeView; selected: boolean; onOpen: (id: NodeId) => void };
type LaneNodeType = Node<LaneData, "lane">;
type FlowEdgeType = Edge<{ state: EdgeState; turn?: number }, "flow">;

const LaneNode = memo(function LaneNode({ data }: NodeProps<LaneNodeType>) {
  return (
    <>
      <Handle type="target" position={Position.Left} id="in" isConnectable={false} />
      <Handle type="target" position={Position.Top} id="up" isConnectable={false} />
      <LaneCard meta={data.meta} view={data.view} selected={data.selected} onOpen={data.onOpen} />
      <Handle type="source" position={Position.Right} id="out" isConnectable={false} />
      <Handle type="source" position={Position.Bottom} id="down" isConnectable={false} />
    </>
  );
});

function FlowEdge({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data }: EdgeProps<FlowEdgeType>) {
  // The edge from Route returns to Reviewer approval runs under the third row, just above the decision row.
  const centerY = data?.turn ? targetY - data.turn : undefined;
  const [path] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 10, offset: 14, centerY });
  const state = data?.state ?? "idle";
  // Dashes flow along an edge while its target waits for or works on what the source handed over; when the
  // target finishes on screen, the ink draws from source to target over the grey line.
  const drawn = useEntered(state, (s) => s === "passed");
  return (
    <>
      <path d={path} className="flow-edge" data-state={state} data-draw={drawn || undefined} />
      {drawn ? <path d={path} className="flow-ink" pathLength={1} /> : null}
    </>
  );
}

const nodeTypes = { lane: LaneNode };
const edgeTypes = { flow: FlowEdge };

function FitOnResize({ target }: { target: React.RefObject<HTMLDivElement | null> }) {
  const flow = useReactFlow();
  useEffect(() => {
    const element = target.current;
    if (!element) return;
    const observer = new ResizeObserver(() => window.requestAnimationFrame(() => flow.fitView(FIT)));
    observer.observe(element);
    return () => observer.disconnect();
  }, [flow, target]);
  return null;
}

export function WorkflowGraph({ view, selected, onOpen, theme }: { view: GraphView; selected: NodeId | null; onOpen: (id: NodeId) => void; theme: "light" | "dark" }) {
  const wrap = useRef<HTMLDivElement>(null);
  // The nodes are React Flow's state, so the sizes it measured stay with them and a step's new state changes only
  // its data. A fresh node list would be measured again, and every edge would leave the page for a frame and
  // restart its flow and its ink.
  const [nodes, setNodes, onNodesChange] = useNodesState<LaneNodeType>(
    NODES.map((meta) => ({
      id: meta.id,
      type: "lane" as const,
      position: POSITIONS[meta.id],
      data: { meta, view: view[meta.id], selected: selected === meta.id, onOpen },
      draggable: false,
      selectable: false,
      focusable: false,
    })),
  );
  useLayoutEffect(() => {
    setNodes((current) => current.map((node) => {
      const id = node.id as NodeId;
      const same = node.data.view === view[id] && node.data.selected === (selected === id) && node.data.onOpen === onOpen;
      return same ? node : { ...node, data: { ...node.data, view: view[id], selected: selected === id, onOpen } };
    }));
  }, [view, selected, onOpen, setNodes]);
  const edges: FlowEdgeType[] = useMemo(
    () => EDGES.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      sourceHandle: edge.sourceHandle,
      targetHandle: edge.targetHandle,
      type: "flow" as const,
      data: { state: edgeState(view, edge), turn: edge.turn },
      focusable: false,
      selectable: false,
    })),
    [view],
  );
  return (
    <div className="graph-wrap" ref={wrap}>
      <ReactFlowProvider>
        <ReactFlow<LaneNodeType, FlowEdgeType>
          nodes={nodes}
          onNodesChange={onNodesChange}
          edges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          colorMode={theme}
          fitView
          fitViewOptions={FIT}
          minZoom={0.35}
          maxZoom={1.6}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          nodesFocusable={false}
          edgesFocusable={false}
          zoomOnScroll={false}
          zoomOnDoubleClick={false}
          panOnScroll={false}
          preventScrolling={false}
          aria-label="Graph of the run steps; select a step for its input and output"
        >
          {/* Top right is empty in this layout, above Route returns; the bottom-right corner keeps the React Flow attribution. */}
          <Controls showInteractive={false} position="top-right" aria-label="Zoom and fit the graph" fitViewOptions={FIT} />
          <FitOnResize target={wrap} />
        </ReactFlow>
      </ReactFlowProvider>
    </div>
  );
}

const PARALLEL: NodeId[] = ["engine_a", "engine_b", "law"];

/** The phone's view of the same run: the steps as a list, in pipeline order, with the same states. The
 * three steps that share a stage are indented; the caption under the list says how they run. */
export function Stepper({ view, selected, onOpen }: { view: GraphView; selected: NodeId | null; onOpen: (id: NodeId) => void }) {
  return (
    <ol className="stepper">
      {NODES.map((meta) => (
        <li key={meta.id} className={PARALLEL.includes(meta.id) ? "parallel" : undefined}>
          <LaneCard meta={META[meta.id]} view={view[meta.id]} selected={selected === meta.id} onOpen={onOpen} />
        </li>
      ))}
    </ol>
  );
}
