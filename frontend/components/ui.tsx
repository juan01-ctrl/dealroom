import { Check, CircleAlert, X } from "lucide-react";
import type { ComponentPropsWithRef, ComponentPropsWithoutRef, ReactNode } from "react";

export function Button({ variant = "default", className = "", type = "button", ...props }: ComponentPropsWithRef<"button"> & {
  variant?: "default" | "primary" | "subtle" | "icon" | "text";
}) {
  const style = variant === "icon" ? "icon-button" : variant === "text" ? "text-button" : `button ${variant}`;
  return <button className={`${style} ${className}`.trim()} type={type} {...props} />;
}

export function Field({ label, className, children }: {
  label: ReactNode; className?: string; children: ReactNode;
}) {
  return <label className={className}>{label}{children}</label>;
}

export function Alert({ children }: { children: ReactNode }) {
  return <div className="alert" role="alert">{children}</div>;
}

export function Status({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`system-note ${className}`.trim()} role="status">{children}</div>;
}

export function PolicyStatus({ outcome, children }: { outcome: "pass" | "blocked" | "needs details"; children: ReactNode }) {
  const Icon = outcome === "pass" ? Check : outcome === "blocked" ? X : CircleAlert;
  return <li className={`policy-check ${outcome === "needs details" ? "needs-details" : outcome}`}>
    <Icon size={13} aria-hidden="true" /><span><strong className="policy-outcome">{outcome === "pass" ? "Passed" : outcome === "blocked" ? "Blocked" : "Needs details"}</strong> {children}</span>
  </li>;
}

export function Panel({ as: Tag = "section", className = "", ...props }: ComponentPropsWithoutRef<"section"> & {
  as?: "aside" | "article" | "section" | "div";
}) {
  return <Tag className={`panel ${className}`.trim()} {...props} />;
}
