"use client";

import { ArrowRight, Beaker, Check, CircleAlert, Clock3, Play, ShieldCheck, X } from "lucide-react";
import { useEffect, useState } from "react";
import Shell from "../../components/shell";
import { Alert, Button, Panel, Status } from "../../components/ui";
import { api, ApiError, capabilities, errorMessage, humanize, openSession, type Run, type Session } from "../../lib";

const metrics = [
  ["scenario_success", "Scenarios passed", "%"], ["business_rule_compliance", "Policy compliance", "%"],
  ["recovery_success", "Recovery success", "%"], ["extraction_accuracy", "Extraction accuracy", "%"],
  ["retrieval_recall", "Evidence recall", "%"], ["retrieval_precision", "Evidence precision", "%"],
  ["sender_evidence_accuracy", "Sender evidence", "%"], ["negotiation_move_accuracy", "Negotiation move", "%"],
  ["average_draft_words", "Average draft length", " words"],
  ["denied_links", "Links blocked", ""],
  ["draft_quality_rubric", "Draft rubric", "%"], ["duplicate_deliveries", "Duplicate deliveries", ""],
  ["unauthorized_attempts", "Unauthorized attempts", ""], ["unauthorized_completed", "Unauthorized completed", ""],
  ["average_latency_ms", "Average latency", " ms"], ["p95_latency_ms", "p95 latency", " ms"],
  ["estimated_cost_usd", "Estimated suite cost", " USD"], ["estimated_cost_per_turn_usd", "Estimated cost per turn", " USD"],
];
const display = (value: unknown) => value === undefined ? "Unavailable" : JSON.stringify(value);

export default function Evaluations() {
  const [session, setSession] = useState<Session>();
  const [identifier, setIdentifier] = useState("");
  const [run, setRun] = useState<Run>();
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);
  const [connecting, setConnecting] = useState(true);
  const [blocked, setBlocked] = useState(false);
  const [readAttempt, setReadAttempt] = useState(0);
  const [sessionAttempt, setSessionAttempt] = useState(0);
  const active = Boolean(identifier) && (!run || ["RUNNING", "QUEUED"].includes(run.status));
  const available = capabilities(session).evaluations;
  const mode = session?.mode;

  useEffect(() => {
    let canceled = false;
    void openSession().then((next) => {
      if (canceled) return;
      setSession(next);
      setIdentifier(sessionStorage.getItem("dealroom-evaluation") || "");
      setBlocked(false);
      setReadAttempt((value) => value + 1);
    }).catch((reason: unknown) => {
      if (!canceled) setError(errorMessage(reason));
    }).finally(() => {
      if (!canceled) setConnecting(false);
    });
    return () => { canceled = true; };
  }, [sessionAttempt]);

  useEffect(() => {
    if (!identifier || connecting) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let read: AbortController | undefined;
    let retry = 1200;
    let complete = false;

    async function refresh() {
      if (complete || document.visibilityState === "hidden") return;
      const controller = new AbortController();
      read = controller;
      try {
        const next = await api.evaluation(identifier, controller.signal);
        if (controller.signal.aborted) return;
        setRun(next);
        setError("");
        setBlocked(false);
        retry = 1200;
        complete = next.status === "COMPLETE" || next.status === "FAILED";
        if (!complete) timer = setTimeout(refresh, 1000);
      } catch (reason) {
        if (controller.signal.aborted) return;
        setError(errorMessage(reason));
        if (reason instanceof ApiError && reason.kind === "missing") {
          sessionStorage.removeItem("dealroom-evaluation");
          setIdentifier("");
          setRun(undefined);
          setBlocked(false);
        } else if (reason instanceof ApiError && reason.kind === "temporary") {
          timer = setTimeout(refresh, retry);
          retry = Math.min(retry * 2, 15000);
        } else {
          setBlocked(true);
        }
      } finally {
        if (read === controller) read = undefined;
      }
    }

    function pause() {
      clearTimeout(timer);
      read?.abort();
    }
    function visibilityChanged() {
      pause();
      void refresh();
    }
    document.addEventListener("visibilitychange", visibilityChanged);
    void refresh();
    return () => {
      document.removeEventListener("visibilitychange", visibilityChanged);
      pause();
    };
  }, [connecting, identifier, readAttempt]);

  function reconnect() {
    setConnecting(true);
    setError("");
    setSessionAttempt((value) => value + 1);
  }

  async function start() {
    setStarting(true);
    setError("");
    try {
      const { id } = await api.startEvaluation();
      sessionStorage.setItem("dealroom-evaluation", id);
      setRun(undefined);
      setIdentifier(id);
    } catch (reason) {
      setError(errorMessage(reason));
      if (reason instanceof ApiError && reason.kind === "session") setBlocked(true);
    } finally { setStarting(false); }
  }

  return <Shell active="evaluations" mode={mode} creator={session?.workspace_kind === "creator"}>
    <main className="workspace eval-workspace">
      <div className="workspace-heading"><div><h1>Negotiation evaluations</h1><p>Run versioned scenarios against the same workflow used in the sandbox.</p></div>
        <Button variant="primary" disabled={!available || connecting || active || starting || blocked} onClick={() => void start()}><Play size={15} /> Run evaluation suite</Button></div>
      {error && <Alert>{error}
        {(!session || blocked) && <Button variant="text" disabled={connecting} onClick={reconnect}>Reconnect</Button>}
      </Alert>}
      {active && <Status><Clock3 size={15} /> {error ? "Waiting for an evaluation update…" : run?.status === "RUNNING" ? "Running the reference suite…" : "Evaluation queued…"}</Status>}
      {run?.status === "FAILED" && <Alert><CircleAlert size={16} /> The worker could not complete the suite. Run it again after the cooldown.</Alert>}
      {!run?.result && !active && <Panel as="div" className="evaluation-empty">
        <div className="empty-icon"><Beaker size={27} strokeWidth={1.45} /></div><h2>Run the reference suite</h2>
        <p>{session && !available ? "Evaluations are disabled in this environment." : "See how the workflow handles pricing, approvals, confidential terms, and interrupted deliveries."}</p>
        <div className="eval-tag"><ShieldCheck size={15} /> {mode === "live" ? "Live model evaluations" : "Scripted workflow verification"}</div>
      </Panel>}
      {run?.result && <>
        <section className="metrics-grid" aria-label="Evaluation metrics">
          {metrics.map(([key, name, unit]) => <Panel as="article" className="metric" key={key}><span>{name}</span>
            <strong>{run.result?.metrics[key] == null ? "—" : run.result.metrics[key] + unit}</strong></Panel>)}
        </section>
        <Panel className="results" aria-label="Scenario results">
          <div className="panel-heading"><Beaker size={16} /><h2>Scenario results</h2><span className="version">{run.result.fixture_version} · {humanize(run.mode)}</span></div>
          {run.result.scenarios.map((scenario) => <details className="scenario" key={scenario.id}>
            <summary><span className={`result-indicator ${scenario.passed ? "pass" : "fail"}`}>{scenario.passed ? <Check size={14} /> : <X size={14} />}</span>
              <span>{scenario.name}</span><span className="scenario-result">{scenario.passed ? "Passed" : "Failed"}</span><ArrowRight size={14} /></summary>
            <div className="scenario-detail">
              {!!scenario.failures.length && <p className="error-text">Failed checks: {scenario.failures.join(", ")}</p>}
              <div className="check-grid"><strong>Check</strong><strong>Expected</strong><strong>Actual</strong>
                {Object.entries({ ...scenario.checks, ...Object.fromEntries(Object.entries(scenario.extraction).map(([key, check]) => ["Extraction · " + key, check])) }).map(([key, check]) =>
                  <div key={key} className={`check-row ${check.passed ? "" : "error-text"}`}><span>{humanize(key)}</span><code>{display(check.expected)}</code><code>{display(check.actual)}</code></div>)}
              </div>
            </div>
          </details>)}
        </Panel>
        <footer className="workspace-footer"><span>{run.mode === "scripted" ? "Scripted results measure workflow behavior." : "Live results depend on the configured model."}</span><span>Latency excludes creator approval time.</span></footer>
      </>}
    </main>
  </Shell>;
}
