import { useEffect, useRef } from "react";

const STAGE_KW = ["[Thread A]", "[Thread B]", "[Thread C]", "Shot detection", "ASR", "Captioning",
  "Scene", "Audio", "Screenwriter", "Editor", "Processing", "[stage]", "[file]"];

function cls(line: string): string {
  const lower = line.toLowerCase();
  if (lower.includes("error") || lower.includes("traceback") || lower.includes("exception") || line.includes("❌")) return "text-red-400";
  if (lower.includes("complete") || lower.includes("done") || lower.includes("success") || line.includes("✅")) return "text-green-400";
  if (STAGE_KW.some((k) => line.includes(k))) return "text-sky-400";
  return "text-neutral-300";
}

export default function JobLog({ lines, height = 320 }: { lines: string[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    const el = ref.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [lines]);

  return (
    <div
      ref={ref}
      style={{ height }}
      className="mt-2 overflow-y-auto rounded-lg border border-neutral-800 bg-black/60 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all"
      onScroll={(e) => {
        const el = e.currentTarget;
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
    >
      {lines.length === 0 ? (
        <span className="text-neutral-600">— 暂无输出 —</span>
      ) : (
        lines.map((l, i) => <div key={i} className={cls(l)}>{l}</div>)
      )}
    </div>
  );
}
