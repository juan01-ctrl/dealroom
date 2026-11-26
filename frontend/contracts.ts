// Browser-facing schema 1. Additive server fields can be ignored by older clients.
export const SCHEMA_VERSION = "1";
type Validator<T> = (value: unknown) => value is T;
type Parsed<T> = T extends Validator<infer Value> ? Value : never;

const text: Validator<string> = (value): value is string => typeof value === "string";
const number: Validator<number> = (value): value is number => typeof value === "number" && Number.isFinite(value);
const integer: Validator<number> = (value): value is number => number(value) && Number.isSafeInteger(value) && value >= 0;
const boolean: Validator<boolean> = (value): value is boolean => typeof value === "boolean";
const unknown: Validator<unknown> = (_value): _value is unknown => true;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function fields<T extends Record<string, Validator<unknown>>>(shape: T) {
  return (value: unknown): value is { [Key in keyof T]: Parsed<T[Key]> } =>
    isRecord(value) && Object.entries(shape).every(([key, validate]) => validate(value[key]));
}

function array<T>(item: Validator<T>): Validator<T[]> {
  return (value): value is T[] => Array.isArray(value) && value.every(item);
}

function record<T>(item: Validator<T>): Validator<Record<string, T>> {
  return (value): value is Record<string, T> => isRecord(value) && Object.values(value).every(item);
}

function nullable<T>(item: Validator<T>): Validator<T | null> {
  return (value): value is T | null => value === null || item(value);
}

function optional<T>(item: Validator<T>): Validator<T | undefined> {
  return (value): value is T | undefined => value === undefined || item(value);
}

function oneOf<const T extends string>(...choices: T[]): Validator<T> {
  return (value): value is T => text(value) && choices.some((choice) => choice === value);
}

export const TONES = ["warm", "concise", "formal"] as const;
export const CURRENCIES = ["USD", "EUR", "GBP", "CAD", "AUD"] as const;
export const isTone = oneOf(...TONES);
export const isCurrency = oneOf(...CURRENCIES);
const mode = oneOf("scripted", "live");

const thread = { id: text, label: text, revision: integer,
  status: oneOf("NEW", "ANALYZING", "AWAITING_APPROVAL", "APPROVED", "SENT", "AWAITING_BRAND_REPLY", "FAILED", "REJECTED", "CLOSED") };
const policy = fields({
  currency: isCurrency, rates: record(integer), minimum_reel: integer, exclusivity_pct: integer,
  lead_days: integer, max_exclusive_days: integer, allow_paid_usage: boolean, tone: isTone,
});
const decision = fields({
  action: oneOf("acceptance", "counteroffer", "clarification", "rejection"),
  total: nullable(integer), base: nullable(integer), surcharge: nullable(integer), currency: nullable(isCurrency),
  questions: array(text), checks: array(text), items: record(integer), advertising: boolean,
});
const proposal = fields({ tone: isTone, invite_followup: boolean });
const mail = fields({ sender: text, recipient: text, subject: text, digest: text });
const draft = fields({
  id: text, version: integer, revision: integer, decision, proposal, body: text,
  status: oneOf("PENDING", "APPROVED", "SENT", "STALE", "REJECTED", "DELIVERY_FAILED"),
  policy_version: integer, created_at: text, mail: optional(nullable(mail)),
});
const history = {
  messages: array(fields({ body: text, revision: integer, created_at: text })),
  drafts: array(draft),
  deliveries: array(fields({ body: text, delivery_key: text, created_at: text,
    revision: optional(nullable(integer)), draft_id: optional(text) })),
};
const entry = fields({ id: integer, revision: integer, kind: text, details: record(unknown), created_at: text });
const recoverableTurn = fields({ job_id: text, revision: integer });
const retrieval = fields({
  sources: array(fields({
    document_id: text, title: text, revision: text, section_ids: array(text), candidate_sections: integer,
    kind: optional(oneOf("registered")),
  })),
  denied_links: integer, latency_ms: number,
});
export const analysisDetails = fields({
  latency_ms: optional(number), cost_usd: optional(nullable(number)),
  usage: optional(nullable(fields({ input_tokens: integer, output_tokens: integer }))),
  retrieval: optional(nullable(retrieval)),
  tools: optional(array(text)), context_ids: optional(array(text)),
  negotiation_move: optional(nullable(text)), sender_evidence: optional(text),
  langfuse_trace_id: optional(text),
});
const check = fields({ expected: unknown, actual: unknown, passed: boolean });
const evaluationResult = fields({
  fixture_version: text, metrics: record(nullable(number)), scenarios: array(fields({
    id: integer, name: text, passed: boolean, failures: array(text), checks: record(check),
    extraction: record(check), quality: record(boolean),
  })),
});

export const schemas = {
  profile: fields({ version: integer, data: policy, suggested_tone: nullable(isTone) }),
  profileUpdate: fields({ version: integer }),
  threads: array(fields(thread)),
  session: fields({
    threads: array(fields(thread)), mode, workspace_kind: optional(oneOf("sandbox", "creator")),
    capabilities: optional(fields({
      evaluations: boolean, simulated_failures: boolean, can_write: optional(boolean),
    })),
  }),
  snapshot: fields({
    ...thread, fault: nullable(oneOf("checkpoint", "delivery")),
    ...history, history_cursor: optional(nullable(text)),
    unresolved_delivery_revision: optional(nullable(integer)),
    recent_receipts: optional(history.deliveries),
    timeline: array(entry),
    recoverable_turns: optional(array(recoverableTurn)),
    timeline_has_more: optional(boolean),
  }),
  history: fields({ ...history, history_cursor: nullable(text) }),
  timeline: fields({ timeline: array(entry), has_more: boolean }),
  identifier: fields({ id: text }),
  inbound: fields({ job_id: text, revision: integer }),
  status: fields({ status: text }),
  evaluation: fields({ id: text, status: oneOf("QUEUED", "RUNNING", "COMPLETE", "FAILED"), mode, result: nullable(evaluationResult) }),
};

export type Policy = Parsed<typeof policy>;
export type Profile = Parsed<typeof schemas.profile>;
export type Proposal = Parsed<typeof proposal>;
export type Draft = Parsed<typeof draft>;
export type Entry = Parsed<typeof entry>;
export type Thread = Parsed<ReturnType<typeof fields<typeof thread>>>;
export type Snapshot = Parsed<typeof schemas.snapshot>;
export type HistoryPage = Parsed<typeof schemas.history>;
export type Session = Parsed<typeof schemas.session>;
export type Run = Parsed<typeof schemas.evaluation>;
export type RecoverableTurn = Parsed<typeof recoverableTurn>;
export type ReviewAction = "approve" | "edit" | "reject";
export type RecoveryAction = "skip" | "retry";
export type Transition = "close" | "reopen" | "reanalyze" | "retry";
export type Fault = "checkpoint" | "delivery";
