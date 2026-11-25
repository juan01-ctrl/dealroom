# Negotiation Quality Implementation Plan

Improve the existing approval-controlled workflow with verified sender personalization and concise, context-dependent negotiation moves. Apply principled negotiation through factual scope and pricing, conditional trades across terms, and a private walk-away threshold. [Harvard Negotiation Project Seven Elements](https://www.pon.harvard.edu/wp-content/uploads/images/posts/Binder-COMPLETE-MC-Nov-20161.pdf) · [Conditional concessions](https://www.pon.harvard.edu/daily/negotiation-skills-daily/negotiating-for-greater-equity-and-diversity-in-the-workplace-nb/)

## 1. Verify the sender before using a first name

- [x] Parse the top-level From header and the sender's closing signature separately. Compare normalized name tokens against the mailbox local part or the signature. When a display name is absent, use a signature only if it matches the mailbox. Use a neutral greeting for conflicting, unsupported or ambiguous candidates. Consider the new reply only and ignore quoted thread text.
- [x] Keep the verified first name in the draft decision so editing preserves the greeting. Show the source classification in the inspector and sanitized telemetry. Keep personal data out of provider drafting inputs and external telemetry.
- [x] Test display-name and email matches, shared mailboxes confirmed by a signature, mailbox-only messages, conflicting signatures, quoted text and multiple From fields.

## 2. Select a negotiation move and write the reply

- [x] Keep deterministic pricing and policy checks authoritative. Use approved outbound context when continuing a negotiation. Shape the reply around the current stage: quote, hold the last delivered quote, reprice changed scope, ask a targeted question, confirm acceptable terms, or decline respectfully.
- [x] Preserve a higher production amount when the brand separates licensing. Give one fact-based explanation for the price or scope. Use an optional conditional scope trade when rights or schedule are the negotiable issues. Never guess a competitor rate, expose private negotiation thresholds, or claim agreement on a term the brand has yet to confirm.
- [x] Turn unresolved terms into one actionable question. Use the verified first name where available. Keep copy short via typed inputs and bounded phrasing. Carry the same stored decision through edit, approval and delivery. Retain the current model call budget and let the live drafting model choose presentation only.
- [x] Test unchanged versus revised scope, an above-target offer with separate licensing, incomplete terms, the word budget, and approval of the exact edited content.

## 3. Verify behavior

- [x] Extend the versioned suite with sender-confidence, quote continuity and separate-budget cases. Evaluate observed greeting status, negotiation move, accepted price, focus and length. Keep deterministic checks separate from subjective review.
- [x] Run PostgreSQL tests, the Next.js production build, and the browser flow with a dedicated scripted database. Review an example reply in context and refresh the evaluation artifact.

Verified with 45 backend tests, 4 browser tests, a production frontend build, and 28 scripted scenarios. The scripted drafts averaged 25.1 words.
