import type { Book, Example, Health, HistoryItem, StreamEvent } from "./types";

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly fields: { field: string; message: string }[] = []) {
    super(message);
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { headers: { accept: "application/json" }, cache: "no-store", signal });
  if (!response.ok) throw new ApiError(`The server answered ${response.status} for ${path}.`, response.status);
  return (await response.json()) as T;
}

export const getHealth = (signal?: AbortSignal) => getJson<Health>("/api/health", signal);
export const getExamples = (signal?: AbortSignal) => getJson<{ examples: Example[]; can_do: string[] }>("/api/examples", signal);
export const getBook = (signal?: AbortSignal) => getJson<Book>("/api/book", signal);

export interface StreamHandlers {
  onEvent: (event: StreamEvent, receivedMs: number) => void;
  onFirstByte?: () => void;
}

/** POST a run and hand each NDJSON event to the caller as it arrives. Resolves when the stream ends. */
export async function streamRun(path: "/api/run" | "/api/approve", body: unknown, handlers: StreamHandlers, signal: AbortSignal): Promise<void> {
  const started = performance.now();
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/x-ndjson" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || !response.body) {
    let message = `The server answered ${response.status}.`;
    let fields: { field: string; message: string }[] = [];
    try {
      const data = (await response.json()) as { error?: string; fields?: { field: string; message: string }[] };
      if (data.error) message = data.error;
      fields = data.fields ?? [];
    } catch {
      // not JSON: keep the status line
    }
    throw new ApiError(message, response.status, fields);
  }
  handlers.onFirstByte?.();
  // Chrome marks a fetch read through a stream reader as canceled when the reader reaches the end before the
  // network layer reports the request complete. A clone read to its end waits for that report.
  const settled = response.clone().arrayBuffer().catch(() => undefined);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline = buffer.indexOf("\n");
    while (newline >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) handlers.onEvent(JSON.parse(line) as StreamEvent, performance.now() - started);
      newline = buffer.indexOf("\n");
    }
  }
  const rest = buffer.trim();
  if (rest) handlers.onEvent(JSON.parse(rest) as StreamEvent, performance.now() - started);
  await settled;
}

interface SessionBody { question: string; history: HistoryItem[]; materiality: string }
// used_ids: how many change ids this session has shown, so a new preview gets a fresh one. /api/run only:
// the decision endpoint refuses fields it does not know.
export interface RunBody extends SessionBody { used_ids?: number }
export interface DecisionBody extends SessionBody {
  run_id: string;
  change: Record<string, unknown>;
  approver: string;
  decision: "approve" | "reject";
  preview_token: string;
  hypothetical: boolean;
}

/** True when events the server spread over time arrived in one burst: some proxy buffered the stream. */
export function wasBuffered(serverMs: number[], receivedMs: number[]): boolean {
  if (serverMs.length < 4) return false;
  const serverSpan = serverMs[serverMs.length - 1] - serverMs[0];
  const receivedSpan = receivedMs[receivedMs.length - 1] - receivedMs[0];
  return serverSpan > 400 && receivedSpan < serverSpan * 0.1;
}
