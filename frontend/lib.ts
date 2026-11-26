import { SCHEMA_VERSION, schemas, type Fault, type Policy, type Proposal, type RecoveryAction, type ReviewAction, type Session, type Transition } from "./contracts";

export type { Draft, Entry, Fault, HistoryPage, Policy, Profile, Proposal, RecoverableTurn, RecoveryAction, ReviewAction, Run, Session, Snapshot, Thread, Transition } from "./contracts";

type ErrorKind = "session" | "conflict" | "missing" | "temporary" | "invalid" | "request";

export class ApiError extends Error {
  constructor(readonly kind: ErrorKind, readonly status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

function errorDetail(result: unknown, fallback: string) {
  if (!result || typeof result !== "object" || !("detail" in result)) return fallback;
  if (typeof result.detail === "string") return result.detail;
  const first: unknown = Array.isArray(result.detail) ? result.detail[0] : undefined;
  return first && typeof first === "object" && "msg" in first && typeof first.msg === "string"
    ? first.msg.replace(/^Value error, /, "") : fallback;
}

function errorKind(status: number): ErrorKind {
  if (status === 401) return "session";
  if (status === 409) return "conflict";
  if (status === 404) return "missing";
  if (status >= 500 || status === 408 || status === 429) return "temporary";
  return "request";
}

type RequestOptions = { method?: "GET" | "POST" | "PUT"; body?: object; signal?: AbortSignal; etag?: string };
type ResponseData<T> = { value: T; etag: string | undefined };

async function request<T>(path: string, validate: (value: unknown) => value is T, options: RequestOptions & { etag: string }): Promise<ResponseData<T> | null>;
async function request<T>(path: string, validate: (value: unknown) => value is T, options?: RequestOptions & { etag?: undefined }): Promise<ResponseData<T>>;
async function request<T>(path: string, validate: (value: unknown) => value is T, options: RequestOptions = {}): Promise<ResponseData<T> | null> {
  const { method = "GET", body, signal, etag } = options;
  let response: Response;
  try {
    response = await fetch("/api" + path, {
      method, signal, cache: "no-store",
      headers: { "X-Sandbox-Request": "1", ...(body && { "Content-Type": "application/json" }), ...(etag && { "If-None-Match": etag }) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (reason) {
    signal?.throwIfAborted();
    throw new ApiError("temporary", 0, reason instanceof TypeError ? "Could not reach the server. Try again." : "The connection failed.");
  }
  if (response.status === 304 && etag) return null;

  const result: unknown = await response.json().catch(() => undefined);
  signal?.throwIfAborted();
  if (!response.ok) {
    throw new ApiError(errorKind(response.status), response.status, errorDetail(result, "The request failed."));
  }
  const schema = response.headers.get("X-Dealroom-Schema");
  if (schema !== null && schema !== SCHEMA_VERSION) {
    throw new ApiError("invalid", response.status, "The workspace needs an update. Reload the page.");
  }
  if (!validate(result)) throw new ApiError("invalid", response.status, "The server returned an invalid response.");
  return { value: result, etag: response.headers.get("ETag") ?? undefined };
}

const threadPath = (id: string) => "/negotiations/" + encodeURIComponent(id);
export const api = {
  async profile(signal?: AbortSignal) {
    return (await request("/profile", schemas.profile, { signal })).value;
  },
  async savePolicy(version: number, policy: Policy) {
    return (await request("/profile", schemas.profileUpdate, { method: "PUT", body: { version, policy } })).value;
  },
  async createThread(event_id: string, label: string) {
    return (await request("/negotiations", schemas.identifier, { method: "POST", body: { event_id, label } })).value;
  },
  snapshot(id: string, signal: AbortSignal, etag?: string) {
    const path = threadPath(id) + "?history=paged";
    return etag ? request(path, schemas.snapshot, { signal, etag })
      : request(path, schemas.snapshot, { signal });
  },
  async history(id: string, before: string, signal: AbortSignal) {
    return (await request(threadPath(id) + "/history?before=" + encodeURIComponent(before), schemas.history, { signal })).value;
  },
  async timeline(id: string, before: number, signal: AbortSignal) {
    return (await request(threadPath(id) + "/timeline?before=" + before, schemas.timeline, { signal })).value;
  },
  inbound(id: string, event_id: string, body: string) {
    return request(threadPath(id) + "/inbound", schemas.inbound, { method: "POST", body: { event_id, body } });
  },
  review(id: string, draftId: string, action: ReviewAction, version: number, proposal?: Proposal, mail_digest?: string) {
    return request(threadPath(id) + "/drafts/" + encodeURIComponent(draftId), schemas.identifier, {
      method: "POST", body: { action, version, proposal, mail_digest },
    });
  },
  fault(id: string, boundary: Fault, revision: number) {
    return request(threadPath(id) + "/failure", schemas.status, { method: "POST", body: { boundary, revision } });
  },
  transition(id: string, action: Transition, revision: number) {
    return request(threadPath(id) + "/transition", schemas.status, { method: "POST", body: { action, revision } });
  },
  recovery(id: string, jobId: string, action: RecoveryAction, revision: number) {
    return request(threadPath(id) + "/recovery", schemas.status, { method: "POST", body: { job_id: jobId, revision, action } });
  },
  async evaluation(id: string, signal?: AbortSignal) {
    return (await request("/evaluations/" + encodeURIComponent(id), schemas.evaluation, { signal })).value;
  },
  async startEvaluation() {
    return (await request("/evaluations", schemas.identifier, { method: "POST" })).value;
  },
};

let pendingSession: Promise<Session> | undefined;
export function openSession() {
  pendingSession ??= request("/session", schemas.session, { method: "POST" })
    .then(({ value }) => value).finally(() => { pendingSession = undefined; });
  return pendingSession;
}

export function capabilities(session: Session | undefined) {
  const scripted = session?.mode === "scripted";
  return session?.capabilities ?? { evaluations: scripted, simulated_failures: scripted };
}

export function errorMessage(reason: unknown) {
  return reason instanceof Error ? reason.message : "The request failed.";
}

export function humanize(value: string) {
  return value.toLowerCase().replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

export function money(minor: number, currency = "USD") {
  return new Intl.NumberFormat("en-US", { style: "currency", currency, maximumFractionDigits: 2,
    minimumFractionDigits: minor % 100 === 0 ? 0 : 2 }).format(minor / 100);
}

export function timeLabel(value: string) {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
