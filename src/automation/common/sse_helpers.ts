/**
 * R156.G — SSE (Server-Sent Events) consumer helper.
 *
 * Single-source-of-truth for SSE event-stream subscription in
 * generated PW tests targeting R156.C-detected SSE endpoints
 * (`/v1/insight/stream`, `/v1/chat/stream`, etc.).
 *
 * Why this helper exists: Playwright's `page.waitForResponse` only
 * catches ONE HTTP response — it cannot consume an SSE stream because
 * SSE keeps the response open and pushes events over the wire-format
 * `data: <payload>\n\n` chunks. EventSource (browser API) doesn't
 * support custom auth headers natively, so we use `fetch()` +
 * ReadableStream parsing inside `page.evaluate` to:
 *   1. Open the stream with `Authorization: Bearer ${AUTH_TOKEN}`
 *   2. Parse the wire-format incrementally
 *   3. Stop on timeout OR max-events cap (operator-bounded; default
 *      10s timeout + 100 events)
 *   4. Return the collected events for assertion
 *
 * R154 non-mutation guarantee: SSE endpoints are READ-only by SSE
 * protocol design (server pushes data; client doesn't mutate state).
 * The fetch call is a GET (or POST when SUT requires init payload,
 * still treated as a token-mint-like read for stream-open).
 *
 * R156.B token chain: `Authorization: Bearer ${AUTH_TOKEN}` reads
 * from process.env.AUTH_TOKEN (populated by R95.1 token precedence +
 * R156.J.3 auto-refresh).
 *
 * Operator-tunable env vars:
 *   ARTA_SSE_DEFAULT_TIMEOUT_MS   -- default 10000 (10s per stream)
 *   ARTA_SSE_DEFAULT_MAX_EVENTS   -- default 100 events per stream
 *   ARTA_SSE_AUTH_HEADER_NAME     -- default "Authorization" (override
 *                                     for SUTs using custom header,
 *                                     e.g., "X-Auth-Token")
 *   ARTA_SSE_AUTH_HEADER_PREFIX   -- default "Bearer " (override e.g.
 *                                     "" for raw-token SUTs)
 *
 * @example
 *   import { subscribeToEventStream, expectSseEvents } from '../common/sse_helpers';
 *   test('Analytics stream emits expected events', async ({ page }) => {
 *     await page.goto('/dashboard');
 *     const events = await subscribeToEventStream(
 *       page, `${API_BASE_URL}/v1/insight/stream?query=abc`,
 *       { timeoutMs: 15000, maxEvents: 20 }
 *     );
 *     await expectSseEvents(events, evs => evs.length > 0, 'stream emitted events');
 *     await expectSseEvents(events, evs => evs.some(e => e.event === 'insight.ready'));
 *   });
 */

import type { Page } from '@playwright/test';

export interface SseEvent {
  /** SSE `event:` field (event type/name); undefined when stream omits it. */
  event?: string;
  /** SSE `data:` field, concatenated across multi-line data blocks. */
  data: string;
  /** SSE `id:` field; undefined when stream omits it. */
  id?: string;
  /** SSE `retry:` field (ms); undefined when stream omits it. */
  retry?: number;
}

export interface SseSubscribeOptions {
  /** Max wallclock to wait for events (default 10000ms). */
  timeoutMs?: number;
  /** Max events to collect before closing the stream (default 100). */
  maxEvents?: number;
  /** Override env-var auth header name (default "Authorization"). */
  authHeaderName?: string;
  /** Override env-var auth header prefix (default "Bearer "). */
  authHeaderPrefix?: string;
  /** Use GET (default) or POST for stream-open. Some SUTs require POST
   *  with an initial-payload body to bind the subscription. */
  method?: 'GET' | 'POST';
  /** When method === 'POST', the init payload. */
  body?: unknown;
}

/**
 * R156.G — subscribe to an SSE endpoint and collect events until
 * timeout OR max-events cap.
 *
 * @param page Playwright page (used to run the fetch inside browser
 *   context so chromium TLS / DNS bridge layers apply).
 * @param url Full URL to the SSE endpoint.
 * @param options See SseSubscribeOptions.
 * @returns Array of SseEvent in arrival order. Empty array when the
 *   stream errors immediately (caller asserts on length).
 */
export async function subscribeToEventStream(
  page: Page,
  url: string,
  options: SseSubscribeOptions = {},
): Promise<SseEvent[]> {
  const timeoutMs =
    options.timeoutMs
    ?? Number(process.env.ARTA_SSE_DEFAULT_TIMEOUT_MS ?? 10000);
  const maxEvents =
    options.maxEvents
    ?? Number(process.env.ARTA_SSE_DEFAULT_MAX_EVENTS ?? 100);
  const authHeaderName =
    options.authHeaderName
    ?? process.env.ARTA_SSE_AUTH_HEADER_NAME
    ?? 'Authorization';
  const authHeaderPrefix =
    options.authHeaderPrefix
    ?? process.env.ARTA_SSE_AUTH_HEADER_PREFIX
    ?? 'Bearer ';
  const token = process.env.AUTH_TOKEN ?? '';
  const method = options.method ?? 'GET';
  const body = method === 'POST' ? JSON.stringify(options.body ?? {}) : null;

  return page.evaluate(
    async (args) => {
      const events: SseEvent[] = [];
      const headers: Record<string, string> = { Accept: 'text/event-stream' };
      if (args.token) {
        headers[args.authHeaderName] = `${args.authHeaderPrefix}${args.token}`;
      }
      if (args.body !== null) {
        headers['Content-Type'] = 'application/json';
      }
      try {
        const resp = await fetch(args.url, {
          method: args.method,
          headers,
          body: args.body,
        });
        if (!resp.ok || !resp.body) {
          // eslint-disable-next-line no-console
          console.warn(
            `[R156.G] SSE subscribe HTTP ${resp.status}; returning 0 events`,
          );
          return events;
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        const deadline = Date.now() + args.timeoutMs;
        // SSE wire-format: events are separated by `\n\n`. Within an
        // event, each line is `<field>: <value>`. Fields we honor:
        // event, data (multi-line concatenated), id, retry.
        while (Date.now() < deadline && events.length < args.maxEvents) {
          const remaining = deadline - Date.now();
          if (remaining <= 0) break;
          const readPromise = reader.read();
          const timeoutPromise = new Promise<{ done: true; value?: undefined }>(
            (resolve) =>
              setTimeout(
                () => resolve({ done: true, value: undefined }),
                remaining,
              ),
          );
          // Race the read against remaining time so we don't hang past
          // the deadline if the SUT never closes the stream.
          const r = (await Promise.race([readPromise, timeoutPromise])) as {
            done: boolean;
            value?: Uint8Array;
          };
          if (r.done) break;
          if (r.value) {
            buffer += decoder.decode(r.value, { stream: true });
          }
          // Drain complete events from buffer (delimited by \n\n).
          const blocks = buffer.split('\n\n');
          buffer = blocks.pop() ?? '';
          for (const block of blocks) {
            const ev: SseEvent = { data: '' };
            const dataLines: string[] = [];
            for (const line of block.split('\n')) {
              if (line.startsWith('event:')) {
                ev.event = line.slice(6).trim();
              } else if (line.startsWith('data:')) {
                dataLines.push(line.slice(5).trim());
              } else if (line.startsWith('id:')) {
                ev.id = line.slice(3).trim();
              } else if (line.startsWith('retry:')) {
                const n = parseInt(line.slice(6).trim(), 10);
                if (!isNaN(n)) ev.retry = n;
              }
            }
            // SSE spec: multi-line data is joined with newlines.
            if (dataLines.length) {
              ev.data = dataLines.join('\n');
              events.push(ev);
              if (events.length >= args.maxEvents) break;
            } else if (ev.event || ev.id || ev.retry !== undefined) {
              // Event with no `data:` field — push it anyway so caller
              // can observe `event: ping` keepalives etc.
              events.push(ev);
              if (events.length >= args.maxEvents) break;
            }
          }
        }
        try {
          await reader.cancel();
        } catch {
          /* silent — best-effort close */
        }
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        // eslint-disable-next-line no-console
        console.warn(`[R156.G] SSE subscribe exception: ${msg}`);
      }
      return events;
    },
    {
      url,
      method,
      body,
      token,
      authHeaderName,
      authHeaderPrefix,
      timeoutMs,
      maxEvents,
    },
  );
}

/**
 * R156.G — predicate-based assertion against collected SSE events.
 *
 * Operator-friendly assertion helper that emits a structured error
 * when the predicate returns false. The `reason` parameter shows up
 * in the test report so operators see WHY the assertion failed.
 */
export async function expectSseEvents(
  events: SseEvent[],
  predicate: (evs: SseEvent[]) => boolean,
  reason?: string,
): Promise<void> {
  if (!predicate(events)) {
    const evsSummary =
      events.length > 0
        ? events
            .slice(0, 3)
            .map(
              (e) =>
                `{event: ${e.event ?? '?'}, data: ${e.data.slice(0, 40)}${
                  e.data.length > 40 ? '...' : ''
                }}`,
            )
            .join(', ')
        : '(no events)';
    throw new Error(
      `[R156.G expectSseEvents] assertion failed: ${reason ?? 'predicate returned false'}. ` +
      `Got ${events.length} events; first 3: ${evsSummary}`,
    );
  }
}
