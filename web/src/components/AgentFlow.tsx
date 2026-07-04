import { Fragment } from "react";
import { CheckOutlined, CloseOutlined, LoadingOutlined, MinusOutlined } from "@ant-design/icons";

export interface FlowStep {
  key: string;
  label: string;
  icon: string; // emoji shown while pending / next
}

export interface StageInfo {
  status: "pending" | "running" | "done" | "error" | "skip" | "next" | string;
  detail?: string;
}

/** Normalize either {status,detail} objects or bare status strings. */
function stageOf(stages: Record<string, any>, key: string): StageInfo {
  const v = stages?.[key];
  if (!v) return { status: "pending" };
  if (typeof v === "string") return { status: v };
  return { status: v.status ?? "pending", detail: v.detail };
}

function DotContent({ status, icon }: { status: string; icon: string }) {
  if (status === "running") return <LoadingOutlined spin />;
  if (status === "done") return <CheckOutlined />;
  if (status === "error") return <CloseOutlined />;
  if (status === "skip") return <MinusOutlined />;
  return <span>{icon}</span>;
}

/**
 * Animated left-to-right agent flow diagram.
 * - connectors flow (animated dashes) into the running / next node
 * - completed nodes pop in a green check
 * - "next" nodes get a dashed accent ring: what the user should do next
 */
export default function AgentFlow({
  steps, stages, onStepClick,
}: {
  steps: FlowStep[];
  stages: Record<string, any>;
  onStepClick?: (key: string) => void;
}) {
  return (
    <div className="flow-wrap">
      {steps.map((s, i) => {
        const st = stageOf(stages, s.key);
        const prev = i > 0 ? stageOf(stages, steps[i - 1].key) : null;
        const prevDone = prev && (prev.status === "done" || prev.status === "skip");
        const connState =
          st.status === "running" || st.status === "next" ? "active"
          : st.status === "done" || st.status === "skip" ? "done"
          : st.status === "error" ? "error"
          : prevDone ? "done"
          : "";
        return (
          <Fragment key={s.key}>
            {i > 0 && <div className={`flow-conn ${connState}`} />}
            <div
              className={`flow-node ${st.status}${onStepClick ? " clickable" : ""}`}
              onClick={onStepClick ? () => onStepClick(s.key) : undefined}
            >
              <div className="flow-dot"><DotContent status={st.status} icon={s.icon} /></div>
              <div className="flow-label">
                {s.label}
                {st.status === "next" && <span className="flow-next-tag">下一步</span>}
              </div>
              <div className="flow-detail" title={st.detail}>{st.detail || " "}</div>
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}
