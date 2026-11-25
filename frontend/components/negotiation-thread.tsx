import { ArrowDownLeft, ArrowRight, Check, ChevronDown, CircleAlert, ClipboardPenLine, ExternalLink, Mail, Send, Sparkles, X } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { isTone, TONES } from "../contracts";
import { humanize, timeLabel, type Draft, type Fault, type Proposal, type RecoverableTurn, type RecoveryAction, type ReviewAction, type Snapshot, type Transition } from "../lib";

import { Button, Field, Panel, Status } from "./ui";

export const REFERENCE = `From: Jordan Lee <jordan@northstar.example>
To: Maya Rivers <maya@creator.example>
Subject: Northstar Summer Launch | Reel partnership

Hi Maya,

I'm reaching out from Northstar about our summer campaign. We'd love to work with you on one Instagram reel. We can offer USD 1,200 for the collaboration, including the rights package in our campaign brief.

Campaign brief: https://briefs.dealroom.example/northstar-summer-launch

Do you have availability for this campaign? Happy to talk through the details.

Best,
Jordan Lee
Partnerships | Northstar`;

export const FOLLOWUP = `From: Jordan Lee <jordan@northstar.example>
To: Maya Rivers <maya@creator.example>
Subject: Re: Northstar Summer Launch | Reel partnership

Hi Maya,

Thanks for putting together a counteroffer. We can offer USD 2,800 for production, with no exclusivity. We now need 30 days of paid social ads. Paid usage is outside that budget.

Could you share a separate usage quote?

Best,
Jordan`;

function BrandEmail({ body, createdAt }: { body: string; createdAt: string }) {
  const separator = body.search(/\r?\n\r?\n/);
  const fields = new Map<string, string>();
  const header = separator < 0 ? [] : body.slice(0, separator).split(/\r?\n/);
  for (const line of header) {
    const field = /^(From|To|Subject):[ \t]*(.*)$/i.exec(line);
    if (field) fields.set(field[1].toLowerCase(), field[2]);
  }
  const content = fields.size ? body.slice(separator).trimStart() : body;
  const urlPattern = /https:\/\/briefs\.dealroom\.example\/([a-z0-9]+(?:-[a-z0-9]+)*)(?![a-z0-9/?#%_-])/g;
  const pieces = [];
  let position = 0;
  for (const match of content.matchAll(urlPattern)) {
    const start = match.index;
    pieces.push(content.slice(position, start));
    pieces.push(<a className="email-link" key={start} href={`/api/briefs/${match[1]}`} target="_blank" rel="noopener noreferrer">
      {match[0]} <ExternalLink size={12} aria-label="Opens in a new tab" />
    </a>);
    position = start + match[0].length;
  }
  pieces.push(content.slice(position));
  return <article className="message">
    <header><span className="message-icon"><ArrowDownLeft size={17} /></span><div>
      <strong>{fields.get("subject") || "Brand email"}</strong>
      <small>Incoming · {timeLabel(createdAt)}</small></div></header>
    {(fields.has("from") || fields.has("to")) && <dl className="email-headers">
      {fields.get("from") && <div><dt>From</dt><dd>{fields.get("from")}</dd></div>}
      {fields.get("to") && <div><dt>To</dt><dd>{fields.get("to")}</dd></div>}
    </dl>}
    <p>{pieces}</p>
  </article>;
}

function DraftCard({ draft, busy, review, creator }: {
  draft: Draft; busy: boolean; creator: boolean; review: (draft: Draft, action: ReviewAction, proposal?: Proposal) => Promise<boolean>;
}) {
  const [editing, setEditing] = useState(false);
  const [proposal, setProposal] = useState(draft.proposal);
  const editButton = useRef<HTMLButtonElement>(null);
  const toneSelect = useRef<HTMLSelectElement>(null);
  const draftHeading = useRef<HTMLElement>(null);
  function close() {
    setEditing(false);
    requestAnimationFrame(() => editButton.current?.focus());
  }
  const canReview = draft.status === "PENDING" || draft.status === "APPROVED";
  async function submit(action: ReviewAction, proposal?: Proposal) {
    if (await review(draft, action, proposal)) {
      setEditing(false);
      draftHeading.current?.focus();
    }
  }

  return <article className="draft-card">
    <header ref={draftHeading} tabIndex={-1}><span className="message-icon draft-icon"><Sparkles size={16} /></span><div><strong>Agent draft</strong><small>Version {draft.version} · {humanize(draft.decision.action)}</small></div>
      <span className={`pill draft-${draft.status.toLowerCase()}`}>{draft.status === "PENDING" ? "Needs review" : humanize(draft.status)}</span>
    </header>
    {draft.mail && <dl className="email-headers">
      <div><dt>From</dt><dd>{draft.mail.sender}</dd></div>
      <div><dt>To</dt><dd>{draft.mail.recipient}</dd></div>
      <div><dt>Subject</dt><dd>{draft.mail.subject}</dd></div>
    </dl>}
    <div className="draft-copy">{draft.body}</div>
    {editing ? <form className="edit-draft" onSubmit={async (e) => {
      e.preventDefault();
      if (!busy && canReview) await submit("edit", proposal);
    }}>
      <Field label="Tone"><select ref={toneSelect} value={proposal.tone} onChange={({ target: { value } }) => { if (isTone(value)) setProposal({ ...proposal, tone: value }); }}>{TONES.map((tone) => <option key={tone}>{tone}</option>)}</select></Field>
      <Field className="check-label" label={null}><input type="checkbox" checked={proposal.invite_followup} onChange={(e) => setProposal({ ...proposal, invite_followup: e.target.checked })} /> Suggest a next step</Field>
      <div className="actions"><Button type="submit" variant="primary" disabled={busy || !canReview}>Save new version</Button><Button disabled={busy} onClick={close}>Cancel</Button></div>
    </form> : canReview && <div className="draft-actions">
      {creator && draft.status === "PENDING" && !draft.mail && <Status>Delivery details are unavailable for this draft.</Status>}
      {draft.status === "PENDING" && (!creator || draft.mail) && <Button variant="primary" disabled={busy} onClick={() => void submit("approve")}><Check size={15} />{draft.mail ? "Approve & send" : "Approve & simulate send"}</Button>}
      <Button ref={editButton} disabled={busy} onClick={() => { setProposal(draft.proposal); setEditing(true); requestAnimationFrame(() => toneSelect.current?.focus()); }}><ClipboardPenLine size={14} />Edit draft</Button>
      <Button variant="subtle" disabled={busy} onClick={() => void submit("reject")}><X size={14} />Reject</Button>
    </div>}
  </article>;
}

export default function NegotiationThread({ snapshot, busy, email, setEmail, send, review, fault, transition, recover, simulateFailures, creator }: {
  snapshot: Snapshot; busy: boolean; email: string; setEmail: (value: string) => void; send: () => void;
  review: (draft: Draft, action: ReviewAction, proposal?: Proposal) => Promise<boolean>;
  fault: (boundary: Fault) => void; transition: (action: Transition) => void;
  recover: (turn: RecoverableTurn, action: RecoveryAction) => void; simulateFailures: boolean; creator: boolean;
}) {
  const [controls, setControls] = useState(false);
  const closed = snapshot.status === "CLOSED";
  const recoverable = snapshot.recoverable_turns ?? [];
  const turns = useMemo(() => {
    type Turn = { message?: Snapshot["messages"][number]; drafts: Draft[] };
    const grouped = new Map<number, Turn>();
    function at(revision: number) {
      const turn = grouped.get(revision) ?? { drafts: [] };
      grouped.set(revision, turn);
      return turn;
    }
    for (const message of snapshot.messages) at(message.revision).message = message;
    for (const draft of snapshot.drafts) at(draft.revision).drafts.push(draft);
    return [...grouped.entries()].sort(([first], [second]) => first - second);
  }, [snapshot.messages, snapshot.drafts]);
  const composer = useRef<HTMLTextAreaElement>(null);

  function loadExample(body: string) {
    setEmail(body);
    composer.current?.focus();
  }

  return <Panel className="thread-panel" aria-label="Negotiation thread">
    <div className="panel-heading"><Mail size={16} /><h2>Negotiation thread</h2>
      <Button variant="subtle" className="fault-button" type="button" onClick={() => setControls(!controls)} aria-expanded={controls}><CircleAlert size={14} /> Recovery controls <ChevronDown size={13} /></Button></div>
    {controls && <div className="recovery-controls">
      {simulateFailures && <>
        <Button disabled={busy || Boolean(snapshot.fault) || closed} onClick={() => fault("checkpoint")}>Fail after checkpoint</Button>
        <Button disabled={busy || Boolean(snapshot.fault) || closed} onClick={() => fault("delivery")}>Fail after delivery</Button>
      </>}
      <Button variant="subtle" disabled={busy} onClick={() => transition(closed ? "reopen" : "close")}>{closed ? "Reopen" : "Close"} negotiation</Button>
      {recoverable.map((turn) => <div className="recoverable-turn" key={turn.job_id}>
        <span>Unprocessed brand email · Revision {turn.revision}</span>
        <Button disabled={busy || closed} onClick={() => recover(turn, "retry")}>Retry revision {turn.revision}</Button>
        <Button variant="subtle" disabled={busy || closed} onClick={() => recover(turn, "skip")}>Skip revision {turn.revision}</Button>
      </div>)}
    </div>}
    {snapshot.fault && <Status><CircleAlert size={15} /> Failure armed after {snapshot.fault === "checkpoint" ? "the next analysis checkpoint" : "the next simulated delivery"}.</Status>}
    <div className="thread-content">
      {!turns.length && <div className="empty-thread">
        <div className="empty-icon"><MessagesIcon /></div>
        {creator ? <h3>Waiting for a brand email</h3> : <>
          <h3>Start a brand negotiation</h3>
          <p>Send a simulated brand email to see how the agent applies Maya’s policy and prepares a draft for review.</p>
          <Button disabled={closed} onClick={() => loadExample(REFERENCE)}>Load example offer <ArrowRight size={14} /></Button>
        </>}
      </div>}
      {turns.map(([revision, { message, drafts }]) => <div key={revision} className="turn">
        {message && <BrandEmail body={message.body} createdAt={message.created_at} />}
        {drafts.map((draft) => <DraftCard key={draft.id} draft={draft} busy={busy} review={review} creator={creator} />)}
      </div>)}
      {snapshot.status === "ANALYZING" && <Status><Sparkles size={15} /> Analyzing offer…</Status>}
      {snapshot.status === "FAILED" && <Status>Processing failed.
        {recoverable.length > 0 && <Button variant="text" onClick={() => setControls(true)}>Review failed turns</Button>}
        <Button variant="text" disabled={busy} onClick={() => transition("retry")}>Retry from checkpoint</Button>
      </Status>}
      {snapshot.status === "REJECTED" && <Status>Draft rejected. <Button variant="text" disabled={busy} onClick={() => transition("reanalyze")}>Generate a fresh draft</Button></Status>}
      {!!snapshot.deliveries.length && <details className="inbox"><summary><Send size={14} /> {creator ? "Delivery history" : "Simulated brand inbox"} <span className="version">{snapshot.deliveries.length}</span></summary>
        {snapshot.deliveries.map((delivery) => <div key={delivery.delivery_key} className="delivered-message"><small>Delivered · {timeLabel(delivery.created_at)}</small><p>{delivery.body}</p></div>)}</details>}
    </div>
    {!creator && <form className="composer" onSubmit={(event) => { event.preventDefault(); send(); }}>
      <div className="composer-title"><label htmlFor="brand-email">Simulate a brand email</label>
        {!!snapshot.messages.length && <Button variant="text" disabled={closed} onClick={() => loadExample(FOLLOWUP)}>Load follow-up</Button>}</div>
      <textarea ref={composer} id="brand-email" value={email} maxLength={4000} onChange={(event) => setEmail(event.target.value)} rows={email.includes("\n") ? 8 : 3}
        disabled={closed} placeholder={closed ? "Reopen this negotiation to continue." : "Type a synthetic offer or load the example above…"} />
      <div className="composer-footer"><span><ShieldIcon /> Synthetic data only</span><Button type="submit" variant="primary" disabled={busy || closed || !email.trim()}><ArrowRight size={16} /> Add to thread</Button></div>
    </form>}
  </Panel>;
}

function MessagesIcon() { return <Mail size={27} strokeWidth={1.45} />; }
function ShieldIcon() { return <CircleAlert size={12} />; }
