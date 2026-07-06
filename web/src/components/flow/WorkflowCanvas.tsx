/**
 * ComfyUI-style workflow canvas for the editing pipeline.
 * Topology: Screenwriter → fan-out to Shot lanes → per-iteration step chains
 * → conflict-check joins per round → rerun segments → final merge.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Background, BackgroundVariant, Controls, MiniMap, Panel, ReactFlow,
  ReactFlowProvider, useEdgesState, useNodesState, useReactFlow,
  type Edge, type Node, type XYPosition,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Crosshair, LayoutGrid, Loader2, Maximize2, Minimize2 } from "lucide-react";
import { api } from "../../api";
import { cn } from "../../lib/utils";
import { groupSteps, entryWorstVerdict, useTrace, type IterEntry, type TaskInfo, type TraceStep } from "../trace";
import { nodeTypes, SW_STEPS } from "./nodes";

// ── graph diffing ─────────────────────────────────────────────────────────────
// buildGraph produces a BRAND-NEW nodes/edges array on every poll (1s). Feeding
// those straight into setRfNodes/setRfEdges (a) clobbered any node the user had
// dragged, and (b) swapped every object for a new reference each second, forcing
// all memoized nodes to re-render and re-mounting the animated edge paths — which
// made the flow animation stutter. The merge helpers below keep object identity
// for anything whose content is unchanged, so only the nodes that actually moved
// forward re-render. This is the Dify model: layout is computed once per topology,
// live polling only patches data — it never touches position or churns identity.

const stripFns = (_k: string, v: unknown) => (typeof v === "function" ? undefined : v);
const nodeSig = (n: Node) => n.type + "|" + JSON.stringify(n.data, stripFns);
const edgeSig = (e: Edge) => JSON.stringify(e);

/** Merge freshly-built nodes into the live canvas without disturbing dragged
 *  positions or re-creating unchanged node objects. */
function mergeNodes(prev: Node[], next: Node[], pins: Map<string, XYPosition>): Node[] {
  const prevById = new Map(prev.map((n) => [n.id, n] as const));
  let changed = prev.length !== next.length;
  const merged = next.map((n) => {
    const old = prevById.get(n.id);
    if (old?.dragging) { changed = true; return old; }   // mid-drag: never disturb
    const pin = pins.get(n.id);
    const pos = pin ?? n.position;
    if (old && old.position.x === pos.x && old.position.y === pos.y && nodeSig(old) === nodeSig(n)) {
      return old;                                          // unchanged → reuse ref
    }
    changed = true;
    return pin ? { ...n, position: pos } : n;
  });
  return changed ? merged : prev;
}

/** Same idea for edges — reuse the old object when nothing changed so React Flow
 *  keeps the SVG path element and its marching-ants animation running smoothly. */
function mergeEdges(prev: Edge[], next: Edge[]): Edge[] {
  const prevById = new Map(prev.map((e) => [e.id, e] as const));
  let changed = prev.length !== next.length;
  const merged = next.map((e) => {
    const old = prevById.get(e.id);
    if (old && edgeSig(old) === edgeSig(e)) return old;
    changed = true;
    return e;
  });
  return changed ? merged : prev;
}

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
const X_ROOT = 2190;         // shot-root column — pushed right to make room for the
                             // asset column + analysis chains + screenwriter + editor

// ── pipeline columns (left → right): audio sources → assets/mix → analysis
//    stages → screenwriter → editor → shots. Edge-to-edge gaps ≈300px (user:
//    the columns felt glued at ~70px).
const AUDIO_SRC_X = -530;              // original music tracks (feed the BGM mix)
const ASSET_X = 10, ASSET_ROW = 196;   // input asset column (group cards / posters)
const STAGE_X = 580, STAGE_ROW = 122;  // batch analysis stage chain (视频理解/音频分析)
const SW_X = 1100, SW_Y = 6;           // "AI 编剧" timeline node (300px wide)
const EDITOR_X = 1700;                 // editor hub, between screenwriter and shots

// batch tasks that render as canvas stage nodes, in dataflow order
export const VIDEO_STAGE_CHAIN = ["video_analysis", "video_clips", "video_dense", "video_scenes"];
export const AUDIO_STAGE_CHAIN = ["audio_analysis_asset", "audio_segments"];
export const CANVAS_STAGE_KEYS = [...VIDEO_STAGE_CHAIN, ...AUDIO_STAGE_CHAIN];

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

export interface AssetInfo {
  path: string;
  file_name: string;
  asset_type: "video" | "image" | "audio";
  annotated: boolean;
  annotation?: Record<string, any>;
  content_hash?: string;
  capture_time?: string | null;   // journey metadata → video clustering
  location?: string | null;
}

export interface ClipInfo { video_path: string; start: string; end: string; duration: number }
export interface ShotInfo {
  section_idx: number;
  shot_idx: number;
  video_path: string;
  is_stitched: boolean;
  fallback: boolean;
  clips: ClipInfo[];
}

function buildGraph(opts: {
  shotTask: TaskInfo | undefined;
  traces: TracesMap;
  expanded: { unit: number; ord: number } | null;
  fullEntries: IterEntry[] | null;
  onToggle: (unit: number, ord: number) => void;
  onOpenScreenwriter?: () => void;
  jobRunning?: boolean;
  onRetryShot?: (sectionIdx: number, shotIdx: number) => void;
  swStage?: string;
  assets?: AssetInfo[];
  onOpenAsset?: (a: AssetInfo) => void;
  shots?: ShotInfo[];
  onOpenClip?: (s: ShotInfo) => void;
  assetLive?: Record<string, string>;   // basename → live analysis state (r/d)
  bgmUsedPaths?: string[];              // source tracks actually used in the BGM mix
  batchTasks?: Record<string, TaskInfo>; // analysis-phase tasks → stage nodes
  onOpenTask?: (task: string) => void;   // open the workbench for a stage
}): { nodes: Node[]; edges: Edge[]; latestActiveId: string | null } {
  const { shotTask, traces, expanded, fullEntries, onToggle, onOpenScreenwriter, jobRunning, onRetryShot, swStage, assets, onOpenAsset, shots, onOpenClip, assetLive, bgmUsedPaths, batchTasks, onOpenTask } = opts;
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

  // "AI 编剧" is a single self-drawn node rendering the whole phase as a
  // station-and-rail timeline (选择音乐段落 → … → 保存分镜脚本), each sub-step a
  // station with its own call data. The phase is "running" while no shots exist
  // yet (total === 0) — more reliable than peeking the last trace phase, which
  // goes quiet BETWEEN sub-steps.
  const swSteps = traces["screenwriter_llm"]?.["0"] ?? [];
  const swFailed = swSteps.some((s) => s.verdict === "fail");
  const swStarted = swSteps.length > 0 || !!swStage;   // has the phase begun?
  const swActive = total === 0 && swStarted;
  // Always render the screenwriter node (like the editor node) so the pipeline
  // topology is visible from the start — a dim "待开始" placeholder before the
  // phase begins, rather than an absent node that looks like something's missing.
  {
    const swEntries = groupSteps(swSteps);
    const calls = swEntries.filter((e) => !e.calling).length;
    const curIdx = swStage ? SW_STEPS.indexOf(swStage) : -1;
    // per-sub-step call data: how many LLM calls + total seconds each step used,
    // grouped by the `stage` stamped on every call event by the backend.
    const byStage: Record<string, { calls: number; elapsed: number }> = {};
    for (const e of swEntries) {
      if (e.calling) continue;
      const b = byStage[e.stage || ""] ?? (byStage[e.stage || ""] = { calls: 0, elapsed: 0 });
      b.calls += 1; b.elapsed += e.elapsed ?? 0;
    }
    // state per sub-step: not started → all pending; before current = done,
    // current = running, after = pending; all done once the editor stage started.
    const stepState = (i: number) => total > 0 ? "d"
      : !swStarted ? "p"
        : curIdx < 0 ? "d"
          : i < curIdx ? "d" : i === curIdx ? (swFailed ? "f" : "r") : "p";
    const steps = SW_STEPS.map((label, i) => ({
      label, state: stepState(i),
      calls: byStage[label]?.calls ?? 0,
      elapsed: Math.round(byStage[label]?.elapsed ?? 0),
    }));
    nodes.push({
      id: "sw", type: "screenwriter", position: { x: SW_X, y: SW_Y },
      data: {
        title: "AI 编剧", running: swActive, started: swStarted,
        state: !swStarted ? "p" : swFailed ? "f" : swActive ? "r" : "d",
        calls, steps, onOpen: onOpenScreenwriter,
      },
    });
    if (swActive) latestActiveId = "sw";
  }

  // Batch analysis phases as stage-node chains: videos → 逐文件调度 → 片段理解
  // → 密集描述 → 场景分析 → 编剧; audio → 音频分析 → 分段描述 → 编剧. The old
  // grids under the canvas are gone in canvas view — clicking a stage opens
  // the workbench. Chains only appear once the job reports those tasks.
  const vChain = VIDEO_STAGE_CHAIN.filter((k) => batchTasks?.[k]);
  const aChain = AUDIO_STAGE_CHAIN.filter((k) => batchTasks?.[k]);
  const stageKind = (t?: TaskInfo): "active" | "done" | "fail" | "pending" => {
    if (!t) return "pending";
    const vals = Object.values(t.states ?? {});
    if (vals.includes("r")) return "active";
    const dn = t.done ?? vals.filter((v) => v === "d").length;
    if (t.total > 0 && dn >= t.total) return "done";
    if (vals.includes("f")) return "fail";
    return dn > 0 ? "done" : "pending";
  };
  const pushChain = (chain: string[], startY: number) => {
    chain.forEach((key, i) => {
      nodes.push({
        id: `stage-${key}`, type: "stage",
        position: { x: STAGE_X, y: startY + i * STAGE_ROW },
        data: { task: key, info: batchTasks![key], onOpen: () => onOpenTask?.(key) },
      });
      if (i > 0) {
        edges.push({
          id: `e-stage-${chain[i - 1]}-${key}`,
          source: `stage-${chain[i - 1]}`, sourceHandle: "chain-out",
          target: `stage-${key}`, targetHandle: "chain-in",
          ...edgeStyle(stageKind(batchTasks![key])),
        });
      }
    });
    // chain tail → screenwriter
    edges.push({
      id: `e-stage-${chain[chain.length - 1]}-sw`,
      source: `stage-${chain[chain.length - 1]}`, sourceHandle: "out", target: "sw",
      ...edgeStyle(stageKind(batchTasks![chain[chain.length - 1]])),
    });
  };
  // video chain centered on the screenwriter, audio chain right below it
  const vChainY = SW_Y + 150 - (vChain.length * STAGE_ROW) / 2;
  if (vChain.length) pushChain(vChain, vChainY);
  if (aChain.length) pushChain(aChain, vChain.length ? vChainY + vChain.length * STAGE_ROW + 44 : SW_Y + 150 - (aChain.length * STAGE_ROW) / 2);
  // entry node of each chain — assets plug in here instead of the screenwriter
  const videoEntry = vChain.length ? `stage-${vChain[0]}` : "sw";
  const audioEntry = aChain.length ? `stage-${aChain[0]}` : "sw";

  // Input assets. Videos/images are CLUSTERED by trip (capture date, >14-day
  // gaps split — same rule as the journey layer) into compact group cards with
  // a thumbnail grid, so 14 sources don't stack a mile high. Music keeps
  // FUSION semantics: when the project audio is an AI mix, the original
  // tracks sit in their own column further left, and ONLY the tracks that
  // actually made it into the mix get a violet edge into the mix node —
  // unused tracks are dimmed and unconnected. The mix alone feeds audio analysis.
  const assetNodeId: Record<number, string> = {};   // global asset idx → canvas node id
  const assetList = assets ?? [];
  if (assetList.length > 0) {
    const _normP = (s: string) => (s || "").replace(/\//g, "\\").toLowerCase();
    const _base = (s: string) => _normP(s).split("\\").pop() ?? "";
    const isMix = (a: AssetInfo) => /bgmmix/i.test(a.file_name || "");
    const mix = assetList.find((a) => a.asset_type === "audio" && isMix(a));
    const audioSrcs = mix ? assetList.filter((a) => a.asset_type === "audio" && !isMix(a)) : [];
    const mainCol = assetList.filter((a) => !audioSrcs.includes(a));
    const visuals = mainCol.filter((a) => a.asset_type !== "audio");
    const mainAudio = mainCol.filter((a) => a.asset_type === "audio");
    const usedSet = new Set((bgmUsedPaths ?? []).map(_base));

    const pushAsset = (a: AssetInfo, id: string, x: number, y: number, dimmed = false) => {
      nodes.push({
        id, type: "asset", position: { x, y },
        style: dimmed ? { opacity: 0.4 } : undefined,
        data: {
          path: a.path, fileName: a.file_name, assetType: a.asset_type,
          annotated: a.annotated, annotation: a.annotation, contentHash: a.content_hash ?? "",
          liveState: assetLive?.[(a.file_name || "").toLowerCase()],
          onOpen: () => onOpenAsset?.(a),
        },
      });
    };

    // ── trip clustering: capture_time (backend) → filename date → 其他 ──
    const fnameDate = (name: string): string | null => {
      const m = (name || "").match(/(20\d{2})[-_]?(\d{2})[-_]?(\d{2})/);
      if (!m) return null;
      const mo = Number(m[2]), dy = Number(m[3]);
      if (mo < 1 || mo > 12 || dy < 1 || dy > 31) return null;
      return `${m[1]}-${m[2]}-${m[3]}`;
    };
    const dateOf = (a: AssetInfo) => (a.capture_time ?? "").slice(0, 10) || fnameDate(a.file_name);
    const dayDiff = (a: string, b: string) => Math.abs(Date.parse(b) - Date.parse(a)) / 86400000;
    const dated = visuals.filter((a) => dateOf(a)).sort((x, y) => dateOf(x)!.localeCompare(dateOf(y)!));
    const undated = visuals.filter((a) => !dateOf(a));
    const groups: AssetInfo[][] = [];
    for (const a of dated) {
      const g = groups[groups.length - 1];
      if (g && dayDiff(dateOf(g[g.length - 1])!, dateOf(a)!) <= 14) g.push(a);
      else groups.push([a]);
    }
    if (undated.length) groups.push(undated);

    const fmtMD = (d: string) => `${Number(d.slice(5, 7))}/${Number(d.slice(8, 10))}`;
    const groupLabel = (g: AssetInfo[]): string => {
      const ds = g.map(dateOf).filter(Boolean) as string[];
      if (!ds.length) return "其他素材";
      const s = ds[0], e = ds[ds.length - 1];
      return s === e ? `${s.slice(0, 4)}年 ${fmtMD(s)}` : `${s.slice(0, 4)}年 ${fmtMD(s)} – ${fmtMD(e)}`;
    };
    const groupSub = (g: AssetInfo[]): string | undefined => {
      const counts = new Map<string, number>();
      for (const a of g) if (a.location) counts.set(a.location, (counts.get(a.location) ?? 0) + 1);
      const top = [...counts.entries()].sort((x, y) => y[1] - x[1])[0];
      return top ? `📍 ${top[0]}` : undefined;
    };
    // rendered card height estimate: header + optional sub + thumb rows
    const groupH = (g: AssetInfo[], sub?: string) =>
      30 + (sub ? 16 : 0) + Math.ceil(g.length / 3) * 49 + 8;

    const COL_GAP = 36;
    const groupMeta = groups.map((g) => { const sub = groupSub(g); return { g, sub, h: groupH(g, sub) }; });
    const colH = groupMeta.reduce((s, m) => s + m.h + COL_GAP, 0) + mainAudio.length * ASSET_ROW;
    let cursorY = SW_Y + 150 - colH / 2;

    groupMeta.forEach((m, gi) => {
      const id = `assetgrp-${gi}`;
      const anyLive = m.g.some((a) => assetLive?.[(a.file_name || "").toLowerCase()] === "r");
      const allDone = m.g.every((a) => a.annotated || assetLive?.[(a.file_name || "").toLowerCase()] === "d");
      nodes.push({
        id, type: "assetGroup", position: { x: ASSET_X, y: cursorY },
        data: {
          label: groupLabel(m.g), sub: m.sub,
          items: m.g.map((a) => ({
            path: a.path, fileName: a.file_name, contentHash: a.content_hash ?? "",
            annotated: a.annotated, live: assetLive?.[(a.file_name || "").toLowerCase()],
            onOpen: () => onOpenAsset?.(a),
          })),
        },
      });
      for (const a of m.g) assetNodeId[assetList.indexOf(a)] = id;
      edges.push({
        id: `e-${id}-${videoEntry}`, source: id, target: videoEntry,
        ...(videoEntry !== "sw" ? { targetHandle: "in" } : {}),
        ...edgeStyle(anyLive ? "active" : allDone ? "done" : "pending"),
      });
      cursorY += m.h + COL_GAP;
    });

    // audio in the main column (the AI mix, or a plain single track)
    mainAudio.forEach((a) => {
      const gi = assetList.indexOf(a);
      const id = `asset-${gi}`;
      assetNodeId[gi] = id;
      pushAsset(a, id, ASSET_X, cursorY);
      cursorY += ASSET_ROW;
      const live = assetLive?.[(a.file_name || "").toLowerCase()];
      edges.push({
        id: `e-${id}-${audioEntry}`, source: id, target: audioEntry,
        ...(audioEntry !== "sw" ? { targetHandle: "in" } : {}),
        ...edgeStyle(live === "r" ? "active" : (a.annotated || live === "d") ? "done" : "pending"),
      });
    });

    if (mix && audioSrcs.length > 0) {
      const mixIdx = assetList.indexOf(mix);
      const mixNodeId = `asset-${mixIdx}`;
      const mixPos = nodes.find((n) => n.id === mixNodeId)?.position;
      const mixY = mixPos?.y ?? cursorY;
      const srcStartY = mixY + 90 - (audioSrcs.length * ASSET_ROW) / 2;
      audioSrcs.forEach((a, i) => {
        const gi = assetList.indexOf(a);
        const id = `asset-${gi}`;
        assetNodeId[gi] = id;
        const used = usedSet.size === 0 || usedSet.has(_base(a.path));
        pushAsset(a, id, AUDIO_SRC_X, srcStartY + i * ASSET_ROW, !used);
        if (used) {
          edges.push({
            id: `e-${id}-mix`, source: id, target: mixNodeId, targetHandle: "in-l",
            animated: false, style: { stroke: "rgba(167,139,250,0.75)", strokeWidth: 1.8 },
          });
        }
      });
    }
  }

  // AI Editor stage node — an explicit anchor for the editor stage: the fan-out
  // hub between the Screenwriter and the per-shot lanes (the lanes ARE the
  // EditorCoreAgent's per-shot selection). Shown from the screenwriter phase on.
  const doneN = Object.values(states).filter((v) => v === "d").length;
  const failN = Object.values(states).filter((v) => v === "f").length;
  const editorRunning = Object.values(states).some((v) => v === "r");
  const editorY = total > 0 ? LANE_TOP + ((total - 1) * LANE_H) / 2 : LANE_TOP;
  nodes.push({
    id: "editor", type: "orchestrator", position: { x: EDITOR_X, y: editorY },
    data: {
      kind: "editor", title: "AI 编辑",
      detail: total > 0
        ? `${doneN}/${total} 镜头已选${failN ? ` · ${failN} 失败` : ""}`
        : swStage ? `编剧中 · ${swStage}` : "等待编剧完成…",
      state: total === 0 ? "p" : doneN === total ? "d" : editorRunning ? "r" : "p",
    },
  });
  if (nodes.some((n) => n.id === "sw")) {
    edges.push({
      id: "e-sw-editor", source: "sw", target: "editor",
      ...edgeStyle(total > 0 ? "done" : swActive ? "active" : "pending"),
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
  // reserve a column for the per-shot "clip result" nodes just before merge
  const shotList = shots ?? [];
  const shotByKey = new Map<string, ShotInfo>();
  for (const s of shotList) shotByKey.set(`${s.section_idx}-${s.shot_idx}`, s);
  const assetList2 = assets ?? [];
  const baseOf = (p: string) => (p || "").split(/[\\/]/).pop()?.toLowerCase() ?? "";
  const assetIndexByPath = (p: string) => {
    const b = baseOf(p);
    return assetList2.findIndex((a) => baseOf(a.file_name) === b || baseOf(a.path) === b);
  };
  const CLIP_COL = shotList.length > 0 ? 250 : 0;
  const clipX = cursor + COL_W * 0.2;
  const mergeX = clipX + CLIP_COL;
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
    const y = LANE_TOP + i * LANE_H;

    // final selected clip (source + time slice) between this shot and the merge
    const label = shotTask?.labels?.[String(i)] ?? "";
    const m = /S(\d+)\D+Shot(\d+)/i.exec(label);
    const shot = m ? shotByKey.get(`${Number(m[1]) - 1}-${Number(m[2]) - 1}`) : undefined;
    let mergeSrc = src;
    if (shot && shot.clips.length > 0) {
      const clipId = `clip-${i}`;
      nodes.push({
        id: clipId, type: "clip", position: { x: clipX, y },
        data: { clips: shot.clips, fallback: shot.fallback, state: st, onOpen: () => onOpenClip?.(shot) },
      });
      edges.push({
        id: `e-${src}-${clipId}`, source: src, target: clipId, targetHandle: "in",
        ...edgeStyle(st === "d" ? (noSteps ? "skip" : "done") : st === "f" ? "fail" : "pending"),
        ...(noSteps && st === "d" ? { label: "缓存跳过", labelStyle: { fill: "#64748b", fontSize: 9 } } : {}),
      });
      // thin, faint curve back to the source asset (leaves the clip's LEFT via
      // the "back" handle, enters the asset's right — a clean leftward arc)
      const ai = assetIndexByPath(shot.clips[0].video_path);
      const backTarget = ai >= 0 ? assetNodeId[ai] : undefined;   // group card or single node
      if (backTarget) {
        edges.push({
          id: `e-${clipId}-${backTarget}`, source: clipId, sourceHandle: "back",
          target: backTarget, animated: false,
          style: { stroke: "rgba(148,163,184,0.16)", strokeWidth: 1 },
        });
      }
      mergeSrc = clipId;
    }
    edges.push({
      id: `e-${mergeSrc}-merge-${i}`, source: mergeSrc, target: "merge",
      ...(mergeSrc.startsWith("clip-") ? { sourceHandle: "out" } : {}),
      ...edgeStyle(st === "d" ? "done" : st === "f" ? "fail" : "pending"),
    });
  }

  return { nodes, edges, latestActiveId };
}

// ── the canvas ──────────────────────────────────────────────────────────────

function CanvasInner({
  jobId, tasks, onOpenScreenwriter, fullscreen, onToggleFullscreen, jobRunning, onRetryShot,
  assets, onOpenAsset, shots, onOpenClip, bgmUsedPaths, onOpenTask,
}: {
  jobId: string;
  tasks: Record<string, TaskInfo>;
  onOpenScreenwriter?: () => void;
  fullscreen: boolean;
  onToggleFullscreen: () => void;
  jobRunning?: boolean;
  onRetryShot?: (sectionIdx: number, shotIdx: number) => void;
  assets?: AssetInfo[];
  onOpenAsset?: (a: AssetInfo) => void;
  shots?: ShotInfo[];
  onOpenClip?: (s: ShotInfo) => void;
  bgmUsedPaths?: string[];
  onOpenTask?: (task: string) => void;
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
  // current screenwriter sub-step label (选择音乐段落 → 生成分镜脚本 → …)
  const swStage = ((tasks["screenwriter_llm"] as any)?.stage_label as string) || "";

  // live per-asset analysis state (视频理解/音乐分析): basename → r/d, so the
  // asset nodes can show "分析中" on the video currently being analyzed.
  const analyzeJson = JSON.stringify([tasks["video_analysis"] ?? null, tasks["audio_analysis_asset"] ?? null]);
  const assetLive = useMemo(() => {
    const m: Record<string, string> = {};
    for (const t of JSON.parse(analyzeJson) as any[]) {
      if (!t) continue;
      const states = t.states ?? {}, labels = t.labels ?? {};
      for (const k of Object.keys(states)) {
        const name = String(labels[k] || "").toLowerCase();
        if (name) m[name] = states[k];
      }
    }
    return m;
  }, [analyzeJson]);
  const analyzingName = useMemo(() => {
    for (const t of JSON.parse(analyzeJson) as any[]) {
      if (!t) continue;
      const states = t.states ?? {}, labels = t.labels ?? {};
      for (const k of Object.keys(states)) if (states[k] === "r") return String(labels[k] || "");
    }
    return "";
  }, [analyzeJson]);

  // analysis-phase tasks → stage nodes (JSON-stabilized against poll churn)
  const batchJson = JSON.stringify(Object.fromEntries(
    CANVAS_STAGE_KEYS.filter((k) => tasks[k]).map((k) => [k, tasks[k]])));
  const batchTasks = useMemo(() => JSON.parse(batchJson) as Record<string, TaskInfo>, [batchJson]);

  // stabilize the assets/shots identity so polling doesn't rebuild every tick
  const assetsJson = JSON.stringify(assets ?? null);
  const assetsStable = useMemo(() => JSON.parse(assetsJson) as AssetInfo[] | null, [assetsJson]);
  const shotsJson = JSON.stringify(shots ?? null);
  const shotsStable = useMemo(() => JSON.parse(shotsJson) as ShotInfo[] | null, [shotsJson]);

  const { nodes, edges, latestActiveId } = useMemo(
    () => buildGraph({
      shotTask: shotTask ?? undefined, traces, expanded, fullEntries, onToggle, onOpenScreenwriter,
      jobRunning, onRetryShot, swStage, assets: assetsStable ?? undefined, onOpenAsset,
      shots: shotsStable ?? undefined, onOpenClip, assetLive, bgmUsedPaths, batchTasks, onOpenTask,
    }),
    [shotTask, traces, expanded, fullEntries, onToggle, onOpenScreenwriter, jobRunning, onRetryShot, swStage, assetsStable, onOpenAsset, shotsStable, onOpenClip, assetLive, bgmUsedPaths, batchTasks, onOpenTask],
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

  // positions the user has hand-placed — these survive the per-poll re-layout
  // until they hit "重新排列". Ref (not state) so recording a drag doesn't itself
  // trigger a re-render.
  const pinned = useRef<Map<string, XYPosition>>(new Map());
  const onNodeDragStart = useCallback(() => { follow.current = false; }, []);
  const onNodeDragStop = useCallback((_: unknown, node: Node) => {
    pinned.current.set(node.id, node.position);
  }, []);
  const relayout = useCallback(() => {
    pinned.current.clear();
    setRfNodes((prev) => prev.map((n) => {
      const fresh = nodes.find((x) => x.id === n.id);
      return fresh ? { ...n, position: fresh.position } : n;
    }));
    window.setTimeout(() => rf.fitView({ duration: 300 }), 30);
  }, [nodes, setRfNodes, rf]);

  useEffect(() => { setRfNodes((prev) => mergeNodes(prev, nodes, pinned.current)); }, [nodes, setRfNodes]);
  useEffect(() => { setRfEdges((prev) => mergeEdges(prev, edges)); }, [edges, setRfEdges]);

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
  // topology jumps from the pre-editor skeleton (assets + screenwriter + editor)
  // to the full shot-lane graph.
  const SW_SKELETON = 2 + (assetsStable?.length ?? 0);
  const prevCount = useRef(0);
  useEffect(() => {
    const prev = prevCount.current;
    prevCount.current = nodes.length;
    if (nodes.length === 0) return;
    if (prev === 0 || (prev <= SW_SKELETON && nodes.length > SW_SKELETON)) {
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
      onNodeDragStart={onNodeDragStart}
      onNodeDragStop={onNodeDragStop}
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
          <button
            className="flex items-center gap-1.5 rounded-lg border border-cyan-500/30 bg-slate-900/85 px-2.5 py-1.5 text-[11px] text-cyan-300 backdrop-blur hover:bg-cyan-500/10"
            onClick={relayout}
            title="清除手动摆放，恢复自动布局"
          >
            <LayoutGrid className="h-3 w-3" /> 重新排列
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
            {analyzingName
              ? <span>正在分析素材：<span className="text-sky-300">{analyzingName}</span> —— 左侧素材列实时显示进度。</span>
              : swStage
                ? <span>AI 编剧进行中：<span className="text-amber-300">{swStage}</span> —— 完成后各镜头会在这里展开。</span>
                : "正在等待编剧阶段…编剧完成后，各镜头会在这里展开。"}
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
  assets?: AssetInfo[];
  onOpenAsset?: (a: AssetInfo) => void;
  shots?: ShotInfo[];
  onOpenClip?: (s: ShotInfo) => void;
  bgmUsedPaths?: string[];
  onOpenTask?: (task: string) => void;
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
