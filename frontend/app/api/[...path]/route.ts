import type { NextRequest } from "next/server";

export const runtime = "nodejs";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  if (!path.every((segment) => /^[a-z0-9-]+$/i.test(segment))) {
    return Response.json({ detail: "Route not found." }, { status: 404 });
  }
  if (request.method !== "GET" && (request.headers.get("X-Sandbox-Request") !== "1" ||
      request.headers.get("Sec-Fetch-Site") === "cross-site")) {
    return Response.json({ detail: "Request must originate from the sandbox." }, { status: 403 });
  }
  const upstream = process.env.BACKEND_URL || "http://127.0.0.1:8000";
  const headers = new Headers();
  for (const name of ["content-type", "cookie", "x-sandbox-request", "sec-fetch-site", "if-none-match", "authorization", "x-workspace-id"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (process.env.API_PROXY_SECRET) {
    headers.set("X-API-Proxy", process.env.API_PROXY_SECRET);
    const client = request.headers.get("x-forwarded-for")?.split(",").at(-1)?.trim();
    if (client && client.length < 64) headers.set("X-Sandbox-Client", client);
  }
  let body: string | undefined;
  if (request.method !== "GET" && request.body) {
    const reader = request.body.getReader();
    const decoder = new TextDecoder();
    body = "";
    let size = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 16384) {
        await reader.cancel();
        return Response.json({ detail: "Request body is too large." }, { status: 413 });
      }
      body += decoder.decode(value, { stream: true });
    }
    body += decoder.decode();
  }
  try {
    const result = await fetch(upstream + "/api/" + path.join("/") + request.nextUrl.search, {
      method: request.method, headers, body, cache: "no-store", signal: AbortSignal.any([request.signal, AbortSignal.timeout(15000)]),
    });
    const returned = new Headers({ "Cache-Control": "no-store" });
    for (const name of ["content-type", "set-cookie", "etag", "x-dealroom-schema", "retry-after"]) {
      const value = result.headers.get(name);
      if (value) returned.set(name, value);
    }
    return new Response(result.status === 304 || result.status === 204 ? null : result.body, { status: result.status, headers: returned });
  } catch {
    return Response.json({ detail: "The sandbox backend is unavailable. Try again." }, { status: 502 });
  }
}

export { proxy as GET, proxy as POST, proxy as PUT };
