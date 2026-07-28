/**
 * api.js — HTTP against the Testra API.
 *
 * Two shapes to deal with, both already established by the server:
 *
 *   • plain JSON      — an error carries FastAPI's `detail`, which may be a
 *                       string, an object (our structured 422s), or Pydantic's
 *                       array of validation errors.
 *   • NDJSON stream   — the long endpoints (publish, analyze) return 200
 *                       immediately and then emit one JSON object per line:
 *                       `{type:"step"}` for progress, `{type:"error"}` for a
 *                       failure that happened after the response opened, and
 *                       `{type:"result"}` last. See backend/services/progress.py.
 *
 * The in-band error is the subtle part: once the stream is open the status code
 * is already 200, so a failure cannot be reported as one. Treating a stream that
 * ends without a result as success would turn every mid-flight failure into a
 * silent no-op, so that case is an error here too.
 */

export class ApiError extends Error {
  constructor(status, detail, fallback) {
    super(ApiError.describe(detail) || fallback);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }

  static describe(detail) {
    if (!detail) return "";
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      // Pydantic validation errors.
      return detail.map((d) => (typeof d === "string" ? d : d?.msg || JSON.stringify(d))).join("; ");
    }
    // Our structured payloads: unsupported project (code/reason/suggestion),
    // quota (message). Render the prose, never the JSON.
    const parts = [detail.reason || detail.message || detail.msg].filter(Boolean);
    if (detail.supported) parts.push(detail.supported);
    if (detail.suggestion) parts.push(detail.suggestion);
    return parts.join(" ");
  }
}

function authHeaders(token) {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function postJson(apiUrl, route, body, token = "") {
  const res = await fetch(new URL(route, apiUrl), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(token) },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  let parsed = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    /* non-JSON body — fall through to the status-based message */
  }
  if (!res.ok) {
    throw new ApiError(res.status, parsed?.detail, `Request failed (HTTP ${res.status})`);
  }
  return parsed;
}

export async function getJson(apiUrl, route, token = "") {
  const res = await fetch(new URL(route, apiUrl), { headers: authHeaders(token) });
  const text = await res.text();
  let parsed = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    /* ignore */
  }
  if (!res.ok) {
    throw new ApiError(res.status, parsed?.detail, `Request failed (HTTP ${res.status})`);
  }
  return parsed;
}

/**
 * POST multipart/form-data and consume an NDJSON progress stream.
 *
 * @param {object} opts
 * @param {Array<[string, string]>} opts.fields   plain form fields
 * @param {{name: string, data: Buffer}} opts.file the archive
 * @param {(step: object) => void} opts.onStep
 * @returns the `result` payload
 */
export async function postStream(apiUrl, route, { fields, file, onStep, token = "" }) {
  const form = new FormData();
  for (const [k, v] of fields) form.append(k, v);
  form.append(
    "file",
    new Blob([file.data], { type: "application/zip" }),
    file.name
  );

  const res = await fetch(new URL(route, apiUrl), {
    method: "POST",
    headers: authHeaders(token),
    body: form,
  });

  // Failures decidable before the stream opened are still real HTTP errors.
  if (!res.ok) {
    const text = await res.text();
    let parsed = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, parsed?.detail, `Request failed (HTTP ${res.status})`);
  }

  let result = null;
  let failure = null;
  for await (const msg of readNdjson(res.body)) {
    if (msg.type === "step") onStep?.(msg);
    else if (msg.type === "result") result = msg.data ?? msg.result ?? msg;
    else if (msg.type === "error") failure = msg;
  }

  if (failure) {
    throw new ApiError(failure.status ?? 500, failure.detail, "The server reported a failure.");
  }
  if (!result) {
    // The connection dropped mid-flight. Reporting success here would make
    // every network blip look like a completed publish.
    throw new Error(
      "The connection closed before the server finished. Check whether the repo " +
        "was created before retrying — publishing twice with the same name will fail."
    );
  }
  return result;
}

/** Yield parsed objects from a newline-delimited JSON body. */
export async function* readNdjson(stream) {
  const decoder = new TextDecoder();
  let buffer = "";
  for await (const chunk of stream) {
    buffer += decoder.decode(chunk, { stream: true });
    let nl;
    // Split on newlines only — a chunk boundary can land mid-object, so the
    // remainder has to stay buffered rather than being parsed as truncated JSON.
    while ((nl = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      try {
        yield JSON.parse(line);
      } catch {
        /* a partial or non-JSON line is not worth failing the run over */
      }
    }
  }
  const tail = buffer.trim();
  if (tail) {
    try {
      yield JSON.parse(tail);
    } catch {
      /* ignore */
    }
  }
}
