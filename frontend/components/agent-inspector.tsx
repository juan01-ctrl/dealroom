import { Activity, Circle, Clock3, FileText, ScanLine, ShieldCheck, Waypoints } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { analysisDetails } from "../contracts";
import { Alert, Button, Panel, PolicyStatus, Status } from "./ui";
import { api, errorMessage, humanize, money, timeLabel, type Entry, type Snapshot } from "../lib";

const names: Record<string, string> = {
  inbound_received: "Brand email received", offer_extracted: "Offer terms extracted",
  policy_checked: "Commercial policy evaluated", draft_created: "Draft saved",
  draft_approved: "Creator approved draft", draft_rejected: "Creator rejected draft",
  simulated_delivery: "Simulated delivery recorded", analysis_completed: "Turn analysis completed",
  delivery_blocked: "Delivery blocked", simulated_failure: "Simulated failure",
  turn_recovery: "Predecessor recovery recorded", job_failed: "Job failed",
  job_retry: "Retry scheduled", job_recovered: "Recovered from failure", failure_armed: "Failure armed",
};

function mergeEvents(...pages: Entry[][]) {
  const events = new Map<number, Entry>();
  for (const page of pages) for (const event of page) events.set(event.id, event);
  return [...events.values()].sort((a, b) => a.id - b.id);
}

export default function AgentInspector({ snapshot }: { snapshot: Snapshot }) {
  const draft = snapshot.drafts.at(-1);
  const currentDraft = draft?.revision === snapshot.revision ? draft : undefined;
  const result = currentDraft?.decision;
  const [history, setHistory] = useState<{ events: Entry[]; hasMore: boolean }>();
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [loadedCount, setLoadedCount] = useState(0);
  const historyRequest = useRef<AbortController | undefined>(undefined);
  const timeline = snapshot.timeline;
  const recent = useMemo(() => (history ? mergeEvents(history.events, timeline) : [...timeline]).reverse(), [history, timeline]);
  const hasMore = history?.hasMore ?? snapshot.timeline_has_more;
  const oldest = recent.at(-1)?.id;

  useEffect(() => () => historyRequest.current?.abort(), []);
  useEffect(() => {
    setHistory((current) => current && ({ ...current, events: mergeEvents(current.events, timeline) }));
  }, [timeline]);

  async function loadOlder() {
    if (oldest === undefined || historyRequest.current) return;
    const controller = new AbortController();
    historyRequest.current = controller;
    setLoadingOlder(true);
    setHistoryError("");
    try {
      const page = await api.timeline(snapshot.id, oldest, controller.signal);
      setHistory((current) => ({
        events: mergeEvents(page.timeline, current?.events ?? timeline),
        hasMore: page.has_more,
      }));
      setLoadedCount(page.timeline.length);
    } catch (reason) {
      if (!controller.signal.aborted) setHistoryError(errorMessage(reason));
    } finally {
      historyRequest.current = undefined;
      if (!controller.signal.aborted) setLoadingOlder(false);
    }
  }
  const analysis = recent.find((entry) => entry.kind === "analysis_completed" && entry.revision === snapshot.revision);
  const extraction = recent.find((entry) => entry.kind === "offer_extracted" && entry.revision === snapshot.revision);
  const evidence = analysisDetails(analysis?.details) ? analysis.details : undefined;
  const { latency_ms: duration, usage, cost_usd: cost, retrieval } = evidence ?? {};
  const outcome = result?.action === "rejection" ? "blocked"
    : result?.action === "clarification" ? "needs details" : "pass";
  return <Panel as="aside" className="inspector" aria-label="Agent inspector">
    <div className="panel-heading"><ScanLine size={17} /><h2>Agent inspector</h2><span className="live-mark" title="Live event updates" /></div>
    <div className="inspector-body">
      <div className="section-title"><Activity size={14} /> Current state</div>
      <div className="state-card"><span className="state-dot" /><strong>{humanize(snapshot.status)}</strong>
        <small>Rev. {snapshot.revision}</small></div>
      {typeof evidence?.negotiation_move === "string" && <dl>
        <div><dt>Negotiation move</dt><dd>{humanize(evidence.negotiation_move)}</dd></div>
        <div><dt>Greeting evidence</dt><dd>{humanize(evidence.sender_evidence ?? "unverified")}</dd></div>
      </dl>}
      {result && <><div className="section-title"><ShieldCheck size={14} /> Policy checks</div>
        <ul className="checks">{result.checks.map((check) => <PolicyStatus key={check} outcome={outcome}>{check}</PolicyStatus>)}</ul>
        {result.total !== null && result.currency && <div className="quote">
          <span>{result.action === "acceptance" ? "Brand offer" : "Proposed amount"}</span>
          <strong>{money(result.total, result.currency)}</strong>
        </div>}</>}
      {retrieval && (retrieval.sources.length > 0 || retrieval.denied_links > 0) && <div className="retrieval-panel">
        <div className="section-title"><FileText size={14} /> Linked brief evidence <small>{retrieval.latency_ms} ms</small></div>
        {retrieval.sources.map((source) => <div className="retrieval-source" key={source.document_id}>
          <a className="email-link" href={`/api/briefs/${source.document_id}`} target="_blank" rel="noopener noreferrer">{source.title}</a>
          <small>Revision {source.revision}</small>
          <small>{source.section_ids.length} of {source.candidate_sections} commercial sections</small>
          <small>{source.section_ids.map(humanize).join(" · ")}</small>
        </div>)}
        {retrieval.denied_links > 0 && <p className="retrieval-warning">{retrieval.denied_links} {retrieval.denied_links === 1 ? "link was" : "links were"} skipped by the retrieval guardrails.</p>}
      </div>}
      {evidence && <details className="evidence">
        <summary><Waypoints size={14} /> Turn evidence</summary>
        <dl><div><dt>Policy version</dt><dd>v{currentDraft?.policy_version}</dd></div>
          <div><dt>Analysis</dt><dd>{typeof duration === "number" ? duration + " ms" : "Unavailable"}</dd></div>
          <div><dt>Tokens</dt><dd>{usage ? usage.input_tokens + usage.output_tokens : "Unavailable"}</dd></div>
          <div><dt>Est. cost</dt><dd>{typeof cost === "number" ? "$" + cost.toFixed(6) : "Unavailable"}</dd></div></dl>
        <p><strong>Tools</strong> {evidence.tools?.map(humanize).join(" · ") || "Unavailable"}</p>
        <p><strong>Context</strong> {evidence.context_ids?.join(", ") || "Current creator policy"}</p>
        {extraction && <pre>{JSON.stringify(extraction.details.terms, null, 2)}</pre>}
      </details>}
      <div className="section-title timeline-title"><Clock3 size={14} /> Event timeline</div>
      {recent.length === 0 ? <div className="empty-timeline">Events appear when a brand email enters the thread.</div> :
        <ol className="timeline" aria-label="Event timeline">
          {recent.map((entry) => <li key={entry.id}><Circle size={9} className={entry.kind.includes("failure") || entry.kind.includes("blocked") ? "warning-icon" : ""} />
            <div><span>{entry.kind === "state_transition" ? humanize(String(entry.details.status)) : names[entry.kind] || humanize(entry.kind)}</span>
              <time dateTime={entry.created_at}>{timeLabel(entry.created_at)}</time></div>
          </li>)}
        </ol>}
      {historyError && <Alert>Could not load timeline history. {historyError}</Alert>}
      {loadedCount > 0 && <Status className="timeline-status">{loadedCount} older events loaded.</Status>}
      {hasMore && <Button className="timeline-more" disabled={loadingOlder || oldest === undefined} onClick={() => void loadOlder()}>{loadingOlder ? "Loading older events…" : "Load older events"}</Button>}
    </div>
  </Panel>;
}
