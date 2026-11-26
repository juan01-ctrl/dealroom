"use client";

import { ChevronDown, Plus } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import AgentInspector from "../components/agent-inspector";
import CreatorProfile from "../components/creator-profile";
import NegotiationThread from "../components/negotiation-thread";
import Shell from "../components/shell";
import { Alert, Button, Status } from "../components/ui";
import { api, ApiError, capabilities, errorMessage, humanize, openSession, type Policy, type Profile, type Session, type Snapshot } from "../lib";

const ACTIVE_POLL_MS = 900;
const INITIAL_IDLE_POLL_MS = 2500;
const MAX_IDLE_POLL_MS = 15000;

export default function Home() {
  const [session, setSession] = useState<Session>();
  const [profile, setProfile] = useState<Profile>();
  const [thread, setThread] = useState("");
  const [orphanedThread, setOrphanedThread] = useState("");
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [emails, setEmails] = useState<Record<string, string>>({});
  const [writeError, setWriteError] = useState("");
  const [readError, setReadError] = useState("");
  const [profileError, setProfileError] = useState("");
  const [initialError, setInitialError] = useState("");
  const [expired, setExpired] = useState(false);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [connecting, setConnecting] = useState(true);
  const [newLabel, setNewLabel] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  const [profileRefresh, setProfileRefresh] = useState(0);
  const [sessionAttempt, setSessionAttempt] = useState(0);
  const pendingInbound = useRef(new Map<string, { id: string; body: string }>());
  const pendingCreate = useRef<{ id: string; label: string } | null>(null);
  const cached = useRef(new Map<string, { value: Snapshot; etag?: string }>());
  const selected = useRef("");
  const writing = useRef(false);
  const threadPicker = useRef<HTMLSelectElement>(null);
  const creator = session?.workspace_kind === "creator";
  const canWrite = session?.capabilities?.can_write ?? !creator;

  const updateProfile = useCallback((next: Profile) => {
    setProfile((current) => current && current.version > next.version ? current : next);
  }, []);
  const requestRefresh = () => setRefresh((value) => value + 1);

  function setEmail(value: string) {
    setEmails((current) => ({ ...current, [thread]: value }));
  }

  function choose(identifier: string) {
    selected.current = identifier;
    setSnapshot(cached.current.get(identifier)?.value);
    setReadError("");
    setThread(identifier);
  }

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const data = await openSession();
        controller.signal.throwIfAborted();
        const policy = await api.profile(controller.signal);
        const previous = selected.current;
        const available = data.threads.some((item) => item.id === previous);
        if (previous && !available) {
          cached.current.clear();
          setOrphanedThread(previous);
          setNotice("The previous negotiation is unavailable in this session.");
        }
        setProfile(policy);
        setProfileError("");
        setSession(data);
        setExpired(false);
        setWriteError("");
        setInitialError("");
        if (!available) choose(data.threads[0]?.id ?? "");
        setRefresh((value) => value + 1);
      } catch (reason) {
        if (!controller.signal.aborted) setInitialError(errorMessage(reason));
      } finally {
        if (!controller.signal.aborted) setConnecting(false);
      }
    })();
    return () => controller.abort();
  }, [sessionAttempt]);

  useEffect(() => {
    if (!thread || expired) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let read: AbortController | undefined;
    let idle = INITIAL_IDLE_POLL_MS;
    let retry = INITIAL_IDLE_POLL_MS;

    async function poll() {
      if (document.visibilityState === "hidden") return;
      const controller = new AbortController();
      read = controller;
      let delay: number;
      try {
        const current = cached.current.get(thread);
        const response = await api.snapshot(thread, controller.signal, current?.etag);
        if (selected.current !== thread || controller.signal.aborted) return;
        if (response) {
          const previous = current?.value;
          const unchanged = previous && (current.etag && current.etag === response.etag ||
            !response.etag && previous.revision === response.value.revision &&
            previous.timeline.at(-1)?.id === response.value.timeline.at(-1)?.id &&
            previous.deliveries.length === response.value.deliveries.length);
          cached.current.set(thread, { ...response, value: unchanged ? previous : response.value });
          if (!unchanged) {
            setSnapshot(response.value);
            idle = INITIAL_IDLE_POLL_MS;
          }
        }
        setReadError("");
        retry = INITIAL_IDLE_POLL_MS;
        const status = cached.current.get(thread)?.value.status;
        delay = status === "ANALYZING" || status === "APPROVED" ? ACTIVE_POLL_MS : idle;
        idle = Math.min(idle * 2, MAX_IDLE_POLL_MS);
      } catch (reason) {
        if (controller.signal.aborted || selected.current !== thread) return;
        setReadError(errorMessage(reason));
        if (reason instanceof ApiError && reason.kind === "session") {
          setExpired(true);
          return;
        }
        delay = retry;
        retry = Math.min(retry * 2, MAX_IDLE_POLL_MS);
      } finally {
        if (read === controller) read = undefined;
      }
      if (document.visibilityState === "visible" && !controller.signal.aborted) timer = setTimeout(poll, delay);
    }

    function pause() {
      clearTimeout(timer);
      read?.abort();
    }

    function visibilityChanged() {
      pause();
      void poll();
    }

    document.addEventListener("visibilitychange", visibilityChanged);
    void poll();
    return () => {
      document.removeEventListener("visibilitychange", visibilityChanged);
      pause();
    };
  }, [expired, refresh, thread]);

  const latestDelivery = snapshot?.deliveries.at(-1)?.delivery_key;
  useEffect(() => {
    if ((!latestDelivery && !profileRefresh) || expired) return;
    const controller = new AbortController();
    void api.profile(controller.signal).then((next) => {
      updateProfile(next);
      setProfileError("");
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      if (reason instanceof ApiError && reason.kind === "session") setExpired(true);
      else setProfileError(errorMessage(reason));
    });
    return () => controller.abort();
  }, [expired, latestDelivery, profileRefresh, updateProfile]);

  async function write<T>(work: () => Promise<T>, success: (result: T) => void, report = true) {
    if (!canWrite || writing.current || expired) return false;
    writing.current = true;
    setBusy(true);
    setWriteError("");
    setNotice("");
    let result: T;
    try {
      result = await work();
    } catch (reason) {
      if (reason instanceof ApiError && reason.kind === "session") setExpired(true);
      if (report) setWriteError(errorMessage(reason));
      else throw reason;
      return false;
    } finally {
      writing.current = false;
      setBusy(false);
    }
    success(result);
    requestRefresh();
    return true;
  }

  function retrySession() {
    setConnecting(true);
    setInitialError("");
    setSessionAttempt((value) => value + 1);
  }

  const active = snapshot?.id === thread ? snapshot : undefined;
  const email = emails[thread] ?? "";
  const locked = busy || expired;
  const previousDraft = emails[orphanedThread];

  function restorePickerFocus() {
    requestAnimationFrame(() => threadPicker.current?.focus());
  }

  return <Shell active="negotiation" mode={session?.mode} creator={creator}>
    <main className="workspace">
      <div className="workspace-heading"><div>
        <h1>Negotiation workspace</h1><p>{canWrite
          ? "Review brand offers, inspect agent decisions, and approve every reply."
          : "You have read-only access to this workspace."}</p>
      </div>{session && <div className="thread-picker">
        {canWrite && newLabel !== undefined ? <form onSubmit={(event) => {
          event.preventDefault();
          if (locked) return;
          const label = newLabel.trim() || "Campaign";
          if (!pendingCreate.current || pendingCreate.current.label !== label) pendingCreate.current = { label, id: crypto.randomUUID() };
          const { id } = pendingCreate.current;
          void write(() => api.createThread(id, label), (created) => {
            pendingCreate.current = null;
            setSession((current) => current && ({
              ...current, threads: current.threads.some((item) => item.id === created.id) ? current.threads
                : [...current.threads, { id: created.id, label, revision: 0, status: "NEW" }],
            }));
            setNewLabel(undefined);
            choose(created.id);
            setNotice("Negotiation created.");
            restorePickerFocus();
          });
        }}><input aria-label="Negotiation name" placeholder="Campaign name" autoFocus maxLength={60} required value={newLabel} onChange={(event) => setNewLabel(event.target.value)} /><Button type="submit" variant="primary" disabled={locked}>Create</Button><Button onClick={() => { setNewLabel(undefined); pendingCreate.current = null; restorePickerFocus(); }}>Cancel</Button></form> :
          <><div className="select-wrap"><select ref={threadPicker} aria-label="Negotiation" value={thread} disabled={busy || !session.threads.length} onChange={(event) => choose(event.target.value)}>
            {session.threads.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select><ChevronDown size={14} aria-hidden="true" /></div>{canWrite && <Button disabled={locked} onClick={() => setNewLabel("")}><Plus size={15} /> New</Button>}</>}
      </div>}</div>
      {notice && <Status className="workspace-notice">{notice}</Status>}
      {expired ? <Alert>Your session expired. <Button variant="text" disabled={connecting} onClick={retrySession}>Reconnect</Button>{initialError}</Alert> : <>
        {initialError && <Alert>{initialError} <Button variant="text" disabled={connecting} onClick={retrySession}>Retry</Button></Alert>}
        {writeError && <Alert>{writeError}</Alert>}
        {readError && <Alert>Could not refresh the negotiation. {readError} <Button variant="text" onClick={requestRefresh}>Refresh</Button></Alert>}
        {profileError && <Alert>Could not refresh the creator profile. {profileError} <Button variant="text" onClick={() => setProfileRefresh((value) => value + 1)}>Refresh profile</Button></Alert>}
      </>}
      {canWrite && !creator && previousDraft && <div className="preserved-draft">
        <label htmlFor="previous-draft">Unsent brand email from the previous session</label>
        <textarea id="previous-draft" readOnly value={previousDraft} rows={3} />
        <Button disabled={locked || !thread || Boolean(email)} onClick={() => {
          setEmail(previousDraft);
          setOrphanedThread("");
        }}>Restore to empty composer</Button>
      </div>}
      {profile && session ? <div className="workspace-grid">
        {active ? <NegotiationThread key={thread} snapshot={active} busy={locked} email={email} setEmail={setEmail}
          creator={creator} canWrite={canWrite} simulateFailures={!creator && capabilities(session).simulated_failures}
          send={() => {
            const body = email.trim();
            if (locked || !body) return;
            const pending = pendingInbound.current.get(thread);
            const id = pending?.body === body ? pending.id : crypto.randomUUID();
            pendingInbound.current.set(thread, { body, id });
            void write(() => api.inbound(thread, id, body), () => {
              pendingInbound.current.delete(thread);
              setEmails((current) => current[thread]?.trim() === body ? { ...current, [thread]: "" } : current);
              setNotice("Brand email added.");
            });
          }}
          review={(draft, choice, proposal) => write(() => api.review(
            thread, draft.id, choice, draft.version, proposal, choice === "approve" ? draft.mail?.digest : undefined,
          ), () => {
            setNotice(choice === "approve" ? "Approval recorded." : choice === "reject" ? "Draft rejected." : "Draft version saved.");
          })}
          fault={(boundary) => void write(() => api.fault(thread, boundary, active.revision), () => setNotice("Failure simulation armed."))}
          transition={(choice) => void write(() => api.transition(thread, choice, active.revision), () => setNotice("Negotiation updated."))}
          recover={(turn, action) => void write(() => api.recovery(thread, turn.job_id, action, active.revision), () => setNotice(action === "retry" ? "Retry scheduled." : "Failed turn skipped."))}
        /> : <div className="loading" role="status">{!thread ? "No negotiations yet." : readError ? "Waiting to refresh the negotiation…" : "Opening negotiation…"}</div>}
        <CreatorProfile profile={profile} creator={creator} canWrite={canWrite} scope={session.threads[0]?.id} busy={locked}
          reload={async () => {
            try {
              const next = await api.profile();
              updateProfile(next);
              return next;
            } catch (reason) {
              if (reason instanceof ApiError && reason.kind === "session") setExpired(true);
              throw reason;
            }
          }}
          save={(version: number, policy: Policy) => write(() => api.savePolicy(version, policy), (saved) => {
            setProfile((current) => current && current.version <= saved.version ? {
              ...current, version: saved.version, data: policy,
              suggested_tone: current.suggested_tone === policy.tone ? null : current.suggested_tone,
            } : current);
            setNotice("Creator policy saved.");
            setProfileRefresh((value) => value + 1);
          }, false)} />
        {active && <AgentInspector key={thread} snapshot={active} />}
      </div> : connecting && <div className="loading" role="status">Opening workspace…</div>}
      <footer className="workspace-footer"><span><span className="live-mark" /> {active ? humanize(active.status) : "Connecting"}</span><span>{creator ? "Creator approval required" : "Simulated delivery · Creator approval required"}</span></footer>
    </main>
  </Shell>;
}
