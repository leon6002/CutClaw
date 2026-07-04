/**
 * ComfyUI-style workflow canvas for the editing pipeline.
 * Topology: Screenwriter → fan-out to Shot lanes → per-iteration step chains
 * → conflict-check joins per round → rerun segments → final merge.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Background, BackgroundVariant, Controls, MiniMap, Panel, ReactFlow,
  ReactFlowProvider, useEdgesState, useNodesState, useReactFlow,
  type Edge, type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Crosshair, Loader2, Maximize2, Minimize2 } from "lucide-react";
import { api } from "../../api";
import { cn } from "../../lib/utils";
import { groupSteps, entryWorstVerdict, useTrace, type IterEntry, type TaskInfo, type TraceStep } from "../trace";
import { nodeTypes } from "./nodes";

// ── data polling ────────────────────────────────────────────────────────────

type TracesMap = Record<string, Record<string, TraceStep[]>>;

function useJobTraces(jobId: string | null) {
  const [traces, setTraces] = useState<TracesMap>({});
  const lastJson = useRef("");
  useEffect(() => {
    lastJson.current = "";
    setTraces({});
    if (!jobId) return;
    let stop = false;
    let timer = 0;
    const tick = async () => {
      try {
        const r = await api<{ traces: TracesMap }>(`/api/jobs/${jobId}/traces`);
        const next = r.traces ?? {};
        // Only re-render when the data actually changed — an unconditional update
        // every second rebuilt the whole graph and made React Flow re-reconcile,
        // which flickered/blanked nodes mid zoom-pan.
        const nextJson = JSON.stringify(next);
        if (!stop && nextJson !== lastJson.current) {
          lastJson.current = nextJson;
          setTraces(next);
        }
      } catch { /* ignore */ }
      if (!stop) timer = window.setTimeout(tick, 1000);
    };
    tick();
    return () => { stop = true; window.clearTimeout(timer); };
  }, [jobId]);
  return traces;
}

// ── layout constants ────────────────────────────────────────────────────────

const COL_W = 265;
const LANE_H = 125;
const LANE_TOP = 130;
const X_ROOT = 290;

interface RoundInfo {
  seq: number; section: number; round: number;
  pending: number[]; winners?: number[]; losers?: number[];
}

function parseRounds(traces: TracesMap): RoundInfo[] {
  const out: RoundInfo[] = [];
  const units = traces["editor_rounds"] ?? {};
  const seqs = Object.keys(units).map(Number).sort((a, b) => a - b);
  for (const seq of seqs) {
    const info: Partial<RoundInfo> = { seq };
    for (const s of units[String(seq)] ?? []) {
      try {
        const p = JSON.parse(s.note ?? "{}");
        if (s.phase === "round_start") {
          info.section = p.section; info.round = p.round; info.pending = p.pending ?? [];
        } else if (s.phase === "round_result") {
          info.winners = p.winners ?? []; info.losers = p.losers ?? [];
        }
      } catch { /* ignore */ }
    }
    if (info.pending) out.push(info as RoundInfo);
  }
  return out;
}

/** Per-shot entries split into round segments (round markers removed). */
function segmentEntries(entries: IterEntry[]): IterEntry[][] {
  const segs: IterEntry[][] = [];
  let cur: IterEntry[] = [];
  let started = false;
  for (const e of entries) {
    if (e.round) {
      if (started) segs.push(cur);
      cur = [];
      started = true;
      continue;
    }
    started = true;
    cur.push(e);
  }
  if (started) segs.push(cur);
  return segs.length ? segs : [[]];
}

// ── graph builder ───────────────────────────────────────────────────────────

function buildGraph(opts: {
  shotTask: TaskInfo | undefined;
  traces: TracesMap;
  expanded: { unit: number; ord: number } | null;
  fullEntries: IterEntry[] | null;
  onToggle: (unit: number, ord: number) => void;
  onOpenScreenwriter?: () => void;
  jobRunning?: boolean;
  onRetryShot?: (sectionIdx: number, shotIdx: number) => void;
}): { nodes: Node[]; edges: Edge[]; latestActiveId: string | null } {
  const { shotTask, traces, expanded, fullEntries, onToggle, onOpenScreenwriter, jobRunning, onRetryShot } = opts;
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  let latestActiveId: string | null = null;

  const total = shotTask?.total ?? 0;
  const states = shotTask?.states ?? {};
  const rounds = parseRounds(traces);

  const edgeStyle = (kind: "active" | "done" | "fail" | "pending" | "skip") => {
    switch (kind) {
      case "active": return { animated: true, style: { stroke: "#22d3ee", strokeWidth: 2 } };
      case "fail": return { animated: false, style: { stroke: "rgba(239,68,68,0.65)", strokeWidth: 1.5 } };
      case "skip": return { animated: false, style: { stroke: "rgba(71,85,105,0.5)", strokeDasharray: "5 4" } };
      case "pending": return { animated: false, style: { stroke: "rgba(71,85,105,0.35)" } };
      default: return { animated: false, style: { stroke: "#475569" } };
    }
  };

  // Screenwriter node (top lane)
  const swSteps = traces["screenwriter_llm"]?.["0"] ?? [];
  const swRunning = swSteps.length > 0 && swSteps[swSteps.length - 1].phase === "calling";
  if (swSteps.length > 0) {
    nodes.push({
      id: "sw", type: "screenwriter", position: { x: 10, y: 6 },
      data: {
        calls: groupSteps(swSteps).filter((e) => !e.calling).length,
        state: swSteps.some((s) => s.verdict === "fail") ? "f" : "d",
        running: swRunning, onOpen: onOpenScreenwriter,
      },
    });
    if (swRunning) latestActiveId = "sw";
  }

  // AI Editor stage node — an explicit anchor for the editor stage: the fan-out
  // hub between the Screenwriter and the per-shot lanes (the lanes ARE the
  // EditorCoreAgent's per-shot selection). Shown from the screenwriter phase on.
  const doneN = Object.values(states).filter((v) => v === "d").length;
  const failN = Object.values(states).filter((v) => v === "f").length;
  const editorRunning = Object.values(states).some((v) => v === "r");
  const editorY = total > 0 ? LANE_TOP + ((total - 1) * LANE_H) / 2 : LANE_TOP;
  nodes.push({
    id: "editor", type: "orchestrator", position: { x: 40, y: editorY },
    data: {
      kind: "editor", title: "AI 编辑",
      detail: total > 0
        ? `${doneN}/${total} 镜头已选${failN ? ` · ${failN} 失败` : ""}`
        : "等待编剧完成…",
      state: total === 0 ? "p" : doneN === total ? "d" : editorRunning ? "r" : "p",
    },
  });
  if (nodes.some((n) => n.id === "sw")) {
    edges.push({
      id: "e-sw-editor", source: "sw", target: "editor",
      ...edgeStyle(total > 0 ? "done" : swRunning ? "active" : "pending"),
    });
  }

  if (total === 0) return { nodes, edges, latestActiveId };

  // per-shot entries → segments
  const shotSegs: IterEntry[][][] = [];
  for (let i = 0; i < total; i++) {
    const steps = traces["editor_shots"]?.[String(i)] ?? [];
    shotSegs.push(segmentEntries(groupSteps(steps)));
  }

  // Each round-depth is now ONE lane column (iterations collapse into pills),
  // so every region is a single COL_W wide + a gap for the conflict node.
  const maxDepth = Math.max(1, ...shotSegs.map((s) => s.length));
  const CONFLICT_GAP = COL_W * 0.95;
  const regionStart: number[] = [];
  let cursor = X_ROOT + COL_W;
  for (let d = 0; d < maxDepth; d++) {
    regionStart.push(cursor);
    cursor += COL_W + CONFLICT_GAP;
  }
  const mergeX = cursor + COL_W * 0.2;
  const centerY = LANE_TOP + ((total - 1) * LANE_H) / 2;

  // shot roots + collapsed iteration lanes (one lane per round-segment)
  const lastNodeOfShot: (string | null)[] = [];
  for (let i = 0; i < total; i++) {
    const st = states[String(i)] ?? "p";
    const y = LANE_TOP + i * LANE_H;
    const rootId = `shot-${i}`;
    const label = shotTask?.labels?.[String(i)] ?? "";
    // labels look like "S1·Shot3·…" — parse the (1-indexed) section/shot so a
    // retry can target the right entry in shot_point.json
    const m = /S(\d+)\D+Shot(\d+)/i.exec(label);
    const canRetry = !jobRunning && !!onRetryShot && (st === "d" || st === "f") && !!m;
    nodes.push({
      id: rootId, type: "shotRoot", position: { x: X_ROOT, y },
      data: {
        idx: i, state: st, label,
        iters: shotTask?.iters?.[String(i)],
        onRetry: canRetry ? () => onRetryShot!(Number(m![1]) - 1, Number(m![2]) - 1) : undefined,
      },
    });
    // fan out from the AI Editor stage node
    edges.push({
      id: `e-editor-${i}`, source: "editor", target: rootId,
      ...edgeStyle(st === "p" ? "pending" : "done"),
    });

    let prevId = rootId;
    let ord = 0;                       // global iteration index across segments
    const segs = shotSegs[i];
    segs.forEach((seg, d) => {
      if (seg.length > 0) {
        const laneId = `lane-${i}-${d}`;
        const isLastSeg = d === segs.length - 1;
        const running = isLastSeg && (st === "r" || !!seg[seg.length - 1]?.calling);
        const ordBase = ord;
        // is the currently-expanded iteration inside this segment?
        const selLocal = (expanded?.unit === i && expanded.ord >= ordBase && expanded.ord < ordBase + seg.length)
          ? expanded.ord - ordBase : null;
        const segFailed = seg.some((e) => !e.calling && entryWorstVerdict(e) === "fail");
        nodes.push({
          id: laneId, type: "shotLane",
          position: { x: regionStart[d], y },
          zIndex: selLocal !== null ? 50 : undefined,
          data: {
            idx: i, state: st,
            entries: seg, ordBase,
            selectedLocal: selLocal,
            fullEntry: selLocal !== null && fullEntries ? fullEntries[expanded!.ord] : undefined,
            running,
            onPill: (globalOrd: number) => onToggle(i, globalOrd),
          },
        });
        edges.push({
          id: `e-${prevId}-${laneId}`, source: prevId, target: laneId,
          ...edgeStyle(running ? "active" : segFailed ? "fail" : "done"),
        });
        if (running) latestActiveId = laneId;
        prevId = laneId;
        ord += seg.length;
      }

      // join into the conflict node of this depth (if a matching round exists)
      const round = rounds.filter((r) => r.pending.includes(i))[d];
      if (round) {
        const cid = `conflict-${round.seq}`;
        if (!nodes.some((n) => n.id === cid)) {
          const resolved = round.winners !== undefined;
          nodes.push({
            id: cid, type: "orchestrator",
            position: { x: regionStart[d] + COL_W + (CONFLICT_GAP - 190) / 2, y: centerY },
            data: {
              kind: "conflict",
              title: `冲突检测 R${round.round}`,
              detail: resolved
                ? `胜出 ${round.winners!.length} · 回炉 ${round.losers!.length}`
                : "等待本轮全部提交…",
              state: resolved ? "d" : "r",
            },
          });
        }
        const lost = round.losers?.includes(i);
        edges.push({
          id: `e-${prevId}-${cid}`, source: prevId, target: cid,
          ...edgeStyle(round.winners === undefined ? (st === "r" ? "active" : "pending") : lost ? "fail" : "done"),
        });
        prevId = cid;
      }
    });
    lastNodeOfShot.push(prevId);
  }

  // merge node
  const doneCount = Object.values(states).filter((v) => v === "d").length;
  const failCount = Object.values(states).filter((v) => v === "f").length;
  nodes.push({
    id: "merge", type: "orchestrator", position: { x: mergeX, y: centerY },
    data: {
      kind: "merge", title: "shot_point 汇总",
      detail: `${doneCount}/${total} 完成${failCount ? ` · ${failCount} 失败` : ""}`,
      state: doneCount === total ? "d" : "r",
    },
  });
  for (let i = 0; i < total; i++) {
    const st = states[String(i)] ?? "p";
    const src = lastNodeOfShot[i];
    if (!src) continue;
    const noSteps = src === `shot-${i}`;
    edges.push({
      id: `e-${src}-merge-${i}`, source: src, target: "merge",
      ...edgeStyle(st === "d" ? (noSteps ? "skip" : "done") : st === "f" ? "fail" : "pending"),
      ...(noSteps && st === "d" ? { label: "缓存跳过", labelStyle: { fill: "#64748b", fontSize: 9 } } : {}),
    });
  }

  return { nodes, edges, latestActiveId };
}

// ── the canvas ──────────────────────────────────────────────────────────────

function CanvasInner({
  jobId, tasks, onOpenScreenwriter, fullscreen, onToggleFullscreen, jobRunning, onRetryShot,
}: {
  jobId: string;
  tasks: Record<string, TaskInfo>;
  onOpenScreenwriter?: () => void;
  fullscreen: boolean;
  onToggleFullscreen: () => void;
  jobRunning?: boolean;
  onRetryShot?: (sectionIdx: number, shotIdx: number) => void;
}) {
  const traces = useJobTraces(jobId);
  const [expanded, setExpanded] = useState<{ unit: number; ord: number } | null>(null);
  const follow = useRef(true);
  const rf = useReactFlow();
  const lastCentered = useRef<string | null>(null);

  // full detail for the expanded unit only
  const { steps: fullSteps } = useTrace(jobId, "editor_shots", expanded?.unit ?? 0, !!expanded);
  const fullEntries = useMemo(
    () => (expanded && fullSteps ? segmentEntries(groupSteps(fullSteps)).flat() : null),
    [expanded, fullSteps],
  );

  const onToggle = useCallback((unit: number, ord: number) => {
    setExpanded((cur) => (cur && cur.unit === unit && cur.ord === ord ? null : { unit, ord }));
    follow.current = false;
  }, []);

  // Stabilize the tasks identity: useJob returns a NEW meta object every poll
  // (1s) even when nothing changed, which used to rebuild + re-sync the whole
  // graph every second.
  const shotTaskJson = JSON.stringify(tasks["editor_shots"] ?? null);
  const shotTask = useMemo(() => JSON.parse(shotTaskJson) as TaskInfo | null, [shotTaskJson]);

  const { nodes, edges, latestActiveId } = useMemo(
    () => buildGraph({
      shotTask: shotTask ?? undefined, traces, expanded, fullEntries, onToggle, onOpenScreenwriter,
      jobRunning, onRetryShot,
    }),
    [shotTask, traces, expanded, fullEntries, onToggle, onOpenScreenwriter, jobRunning, onRetryShot],
  );

  // React Flow v12 controlled mode REQUIRES onNodesChange: node dimension
  // measurements arrive as change events, and with a bare `nodes` prop they
  // were silently dropped. When a re-sync landed at the wrong moment, static
  // nodes (done/failed shots, screenwriter, merge) lost their measurements and
  // vanished from the canvas — only the actively-updating nodes survived,
  // because their data churn forced re-measurement. useNodesState applies the
  // measurement changes properly.
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<Node>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<Edge>([]);
  useEffect(() => { setRfNodes(nodes); }, [nodes, setRfNodes]);
  useEffect(() => { setRfEdges(edges); }, [edges, setRfEdges]);

  // re-fit the graph when the container resizes between inline / fullscreen
  const firstFs = useRef(true);
  useEffect(() => {
    if (firstFs.current) { firstFs.current = false; return; }
    const id = window.setTimeout(() => rf.fitView({ duration: 300 }), 80);
    return () => window.clearTimeout(id);
  }, [fullscreen, rf]);

  // Initial fit: nodes stream in AFTER mount (rfNodes starts empty), so the
  // built-in `fitView` prop fires on an empty canvas and the graph appears
  // tiny in a corner. Fit when content first arrives, and again when the
  // topology jumps from the 2-node skeleton (sw + editor) to the full
  // shot-lane graph.
  const prevCount = useRef(0);
  useEffect(() => {
    const prev = prevCount.current;
    prevCount.current = nodes.length;
    if (nodes.length === 0) return;
    if (prev === 0 || (prev <= 3 && nodes.length > 3)) {
      // small delay so React Flow has measured the freshly-added nodes
      const id = window.setTimeout(() => rf.fitView({ duration: 300, maxZoom: 0.95 }), 80);
      return () => window.clearTimeout(id);
    }
  }, [nodes.length, rf]);

  // auto-follow the newest active node
  useEffect(() => {
    if (!follow.current || !latestActiveId || latestActiveId === lastCentered.current) return;
    const n = nodes.find((x) => x.id === latestActiveId);
    if (n) {
      rf.setCenter(n.position.x + 130, n.position.y + 40, { zoom: Math.max(rf.getZoom(), 0.75), duration: 500 });
      lastCentered.current = latestActiveId;
    }
  }, [latestActiveId, nodes, rf]);

  return (
    <ReactFlow
      nodes={rfNodes}
      edges={rfEdges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      nodeTypes={nodeTypes}
      fitView
      minZoom={0.15}
      maxZoom={1.75}
      proOptions={{ hideAttribution: true }}
      onMoveStart={(ev) => { if (ev) follow.current = false; }}
      onPaneClick={() => setExpanded(null)}
      nodesConnectable={false}
      deleteKeyCode={null}
    >
      <Background variant={BackgroundVariant.Dots} gap={26} size={1} color="rgba(255,255,255,0.09)" />
      <Controls position="bottom-left" showInteractive={false} />
      <MiniMap
        position="bottom-right" pannable zoomable
        nodeColor={(n) => {
          const s = (n.data as any)?.state ?? ((n.data as any)?.isCurrent ? "r" : "");
          return s === "d" ? "#34d399" : s === "r" ? "#22d3ee" : s === "f" ? "#f87171" : "#334155";
        }}
        maskColor="rgba(2,6,18,0.75)"
        style={{ background: "#0b1220" }}
      />
      <Panel position="top-right">
        <div className="flex flex-col items-end gap-1.5">
          <button
            className="flex items-center gap-1.5 rounded-lg border border-cyan-500/30 bg-slate-900/85 px-2.5 py-1.5 text-[11px] text-cyan-300 backdrop-blur hover:bg-cyan-500/10"
            onClick={onToggleFullscreen}
            title={fullscreen ? "退出全屏 (Esc)" : "全屏"}
          >
            {fullscreen ? <Minimize2 className="h-3 w-3" /> : <Maximize2 className="h-3 w-3" />}
            {fullscreen ? "退出全屏" : "全屏"}
          </button>
          <button
            className="flex items-center gap-1.5 rounded-lg border border-cyan-500/30 bg-slate-900/85 px-2.5 py-1.5 text-[11px] text-cyan-300 backdrop-blur hover:bg-cyan-500/10"
            onClick={() => {
              follow.current = true;
              lastCentered.current = null;
              if (latestActiveId) {
                const n = nodes.find((x) => x.id === latestActiveId);
                if (n) rf.setCenter(n.position.x + 130, n.position.y + 40, { zoom: 0.85, duration: 500 });
              } else {
                rf.fitView({ duration: 500 });
              }
            }}
          >
            <Crosshair className="h-3 w-3" /> 回到最新
          </button>
        </div>
      </Panel>
      {/* No shot data yet (job switching, or screenwriter phase before the
          editor produces shots) — show a clear loading hint instead of what
          looks like a lone broken node. */}
      {!tasks["editor_shots"]?.total && (
        <Panel position="top-center">
          <div className="mt-10 flex items-center gap-2 rounded-lg border border-white/10 bg-slate-900/85 px-4 py-2.5 text-xs text-slate-400 backdrop-blur">
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-cyan-400" />
            正在等待编辑阶段生成镜头…编剧完成后，各镜头会在这里展开。
          </div>
        </Panel>
      )}
    </ReactFlow>
  );
}

export default function WorkflowCanvas(props: {
  jobId: string;
  tasks: Record<string, TaskInfo>;
  onOpenScreenwriter?: () => void;
  jobRunning?: boolean;
  onRetryShot?: (sectionIdx: number, shotIdx: number) => void;
  overlay?: ReactNode;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [fullscreen, setFullscreen] = useState(false);

  // Native Fullscreen API instead of a `position: fixed` overlay: an ancestor
  // Card has `backdrop-filter`, which makes `fixed` resolve against the Card
  // (not the viewport). requestFullscreen promotes the element to the browser's
  // top layer, escaping that containing block — and, since the DOM node never
  // moves, CanvasInner (trace polling, expanded node) is NOT remounted on toggle.
  useEffect(() => {
    const onChange = () => setFullscreen(document.fullscreenElement === wrapRef.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      document.exitFullscreen?.();          // native Esc also triggers this path
    } else {
      wrapRef.current?.requestFullscreen?.();
    }
  }, []);

  return (
    <div
      ref={wrapRef}
      className={cn(
        "relative overflow-hidden bg-[#070b14]",
        fullscreen
          ? "h-screen w-screen rounded-none border-0"
          : "h-[68vh] min-h-[420px] rounded-xl border border-white/[0.08]",
      )}
    >
      <ReactFlowProvider>
        <CanvasInner {...props} fullscreen={fullscreen} onToggleFullscreen={toggleFullscreen} />
      </ReactFlowProvider>
      {/* in-canvas overlay (e.g. Agent workbench) — lives INSIDE the wrapper so
          it stays visible in fullscreen mode too */}
      {props.overlay && (
        <div className="absolute inset-y-3 right-3 z-30 flex w-[52%] min-w-[440px] max-w-[780px]">
          {props.overlay}
        </div>
      )}
    </div>
  );
}
