import { LockKeyhole, Settings2, UserRound } from "lucide-react";
import { useRef, useState } from "react";
import { CURRENCIES, isCurrency, isTone, TONES } from "../contracts";
import { ApiError, errorMessage, money, type Policy, type Profile } from "../lib";
import { Alert, Button, Field, Panel } from "./ui";

export default function CreatorProfile({ profile, creator, scope, busy, save, reload }: {
  profile: Profile; creator: boolean; scope: string | undefined; busy: boolean;
  save: (version: number, policy: Policy) => Promise<boolean>;
  reload: () => Promise<Profile>;
}) {
  const [editor, setEditor] = useState<{ version: number; policy: Policy; scope: string | undefined }>();
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState("");
  const [reloading, setReloading] = useState(false);
  const editButton = useRef<HTMLButtonElement>(null);
  const currency = useRef<HTMLSelectElement>(null);
  const reloadButton = useRef<HTMLButtonElement>(null);
  const policy = editor?.policy ?? profile.data;
  const scopeChanged = Boolean(editor && editor.scope !== scope);

  function edit(tone?: Policy["tone"]) {
    setEditor({ version: profile.version, policy: { ...profile.data, tone: tone ?? profile.data.tone }, scope });
    setConflict(false);
    setError("");
    requestAnimationFrame(() => currency.current?.focus());
  }

  function setPolicy(policy: Policy) {
    setEditor((current) => current && { ...current, policy });
  }

  function close() {
    setEditor(undefined);
    requestAnimationFrame(() => editButton.current?.focus());
  }

  async function loadLatest() {
    setReloading(true);
    setError("");
    try {
      const latest = await reload();
      setEditor({ version: latest.version, policy: latest.data, scope });
      setConflict(false);
      requestAnimationFrame(() => currency.current?.focus());
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setReloading(false);
    }
  }

  return <Panel as="aside" className="profile-panel" aria-label="Creator profile">
    <div className="panel-heading"><UserRound size={16} /><h2>Creator profile</h2><Button ref={editButton} variant="icon" title="Edit policy" aria-label="Edit policy" disabled={Boolean(editor) || busy} onClick={() => edit()}><Settings2 size={16} /></Button></div>
    <div className="profile-body">
      {!creator && <div className="creator"><div className="avatar">MR</div><div><strong>Maya Rivers</strong><span>Content creator</span></div></div>}
      {editor ? <form className="profile-form" onSubmit={async (event) => {
        event.preventDefault();
        if (busy || reloading || conflict || scopeChanged) return;
        setError("");
        try {
          if (await save(editor.version, editor.policy)) close();
        } catch (reason) {
          if (reason instanceof ApiError && reason.kind === "conflict") {
            setConflict(true);
            requestAnimationFrame(() => reloadButton.current?.focus());
          } else setError(errorMessage(reason));
        }
      }}>
        {(conflict || scopeChanged) && <Alert>The policy has changed. Load the latest policy to replace these edits.
          <Button ref={reloadButton} variant="text" disabled={busy || reloading} onClick={() => void loadLatest()}>Load latest policy</Button>
        </Alert>}
        {error && <Alert>{error}</Alert>}
        <fieldset className="policy-fields" disabled={busy || reloading}>
          <Field label="Currency"><select ref={currency} value={policy.currency} onChange={({ target: { value } }) => { if (isCurrency(value)) setPolicy({ ...policy, currency: value }); }}>
            {CURRENCIES.map((value) => <option key={value}>{value}</option>)}
          </select></Field>
          {Object.entries(policy.rates).map(([item, value]) => <Field key={item} label={item[0].toUpperCase() + item.slice(1) + " rate"}><input required type="number" min="0.01" step="0.01" value={value / 100} onChange={(e) => setPolicy({ ...policy, rates: { ...policy.rates, [item]: Math.round(Number(e.target.value) * 100) } })} /></Field>)}
          <Field label="Minimum reel rate"><input required type="number" min="0.01" step="0.01" value={policy.minimum_reel / 100} onChange={(e) => setPolicy({ ...policy, minimum_reel: Math.round(Number(e.target.value) * 100) })} /></Field>
          <Field label="Exclusivity surcharge (%)"><input required type="number" min="0" max="300" value={policy.exclusivity_pct} onChange={(e) => setPolicy({ ...policy, exclusivity_pct: Number(e.target.value) })} /></Field>
          <Field label="Minimum lead time (days)"><input required type="number" min="0" max="3650" value={policy.lead_days} onChange={(e) => setPolicy({ ...policy, lead_days: Number(e.target.value) })} /></Field>
          <Field label="Maximum exclusivity (days)"><input required type="number" min="0" max="3650" value={policy.max_exclusive_days} onChange={(e) => setPolicy({ ...policy, max_exclusive_days: Number(e.target.value) })} /></Field>
          <Field label="Default tone"><select value={policy.tone} onChange={({ target: { value } }) => { if (isTone(value)) setPolicy({ ...policy, tone: value }); }}>{TONES.map((tone) => <option key={tone}>{tone}</option>)}</select></Field>
          <Field className="check-label" label={null}><input type="checkbox" checked={policy.allow_paid_usage} onChange={(e) => setPolicy({ ...policy, allow_paid_usage: e.target.checked })} /> Allow separately priced paid usage</Field>
        </fieldset>
        <div className="actions"><Button type="submit" variant="primary" disabled={busy || reloading || conflict || scopeChanged}>Save policy</Button><Button disabled={busy || reloading} onClick={close}>Cancel</Button></div>
      </form> : <>
        <div className="side-section"><div className="eyebrow">Commercial policy <span className="version">v{profile.version}</span></div>
          <dl><div><dt>Standard reel</dt><dd>{money(profile.data.rates.reel, profile.data.currency)}</dd></div>
          <div><dt><LockKeyhole size={11} /> Minimum reel</dt><dd>{money(profile.data.minimum_reel, profile.data.currency)}</dd></div>
          <div><dt>Exclusivity</dt><dd>+{profile.data.exclusivity_pct}%</dd></div>
          <div><dt>Paid usage</dt><dd>{profile.data.allow_paid_usage ? "Separate" : "Restricted"}</dd></div></dl>
        </div>
        <div className="side-section"><div className="eyebrow">Preferences</div><dl>
          <div><dt>Lead time</dt><dd>{profile.data.lead_days} days</dd></div>
          <div><dt>Max. exclusivity</dt><dd>{profile.data.max_exclusive_days} days</dd></div>
          <div><dt>Tone</dt><dd className="capitalize">{profile.data.tone}</dd></div></dl></div>
        {profile.suggested_tone && <div className="privacy"><span>Suggested tone: {profile.suggested_tone}. <Button variant="text" disabled={busy} onClick={() => edit(profile.suggested_tone ?? undefined)}>Review suggestion</Button></span></div>}
        <Button className="full" disabled={busy} onClick={() => edit()}>Edit creator policy</Button>
        <div className="privacy"><LockKeyhole size={13} /><span>{creator ? "Pricing floors are private to this workspace." : "Pricing floors are private to the creator."}</span></div>
      </>}
    </div>
  </Panel>;
}
