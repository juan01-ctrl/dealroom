import Link from "next/link";
import { Activity, ArrowUpRight, Beaker, MessagesSquare, Radio } from "lucide-react";

export default function Shell({ active, mode, creator = false, children }: {
  active: "negotiation" | "evaluations"; mode?: string; creator?: boolean; children: React.ReactNode;
}) {
  return <>
    <header className="topbar">
      <Link className="brand" href="/"><span className="brand-icon"><Activity size={19} /></span>Dealroom<span className="brand-divider" /><span className="brand-context">{creator ? "Creator" : "Sandbox"}</span></Link>
      <nav aria-label="Workspace">
        <Link className={active === "negotiation" ? "selected" : ""} aria-current={active === "negotiation" ? "page" : undefined} href="/"><MessagesSquare size={15} /> Negotiation</Link>
        <Link className={active === "evaluations" ? "selected" : ""} aria-current={active === "evaluations" ? "page" : undefined} href="/evaluations"><Beaker size={15} /> Evaluations</Link>
      </nav>
      <div className="environment"><Radio size={14} /> <span>{creator ? "Creator workspace" : mode === "live" ? "Live model · simulated delivery" : "Scripted simulation"}</span><ArrowUpRight size={13} /></div>
    </header>
    {children}
  </>;
}
