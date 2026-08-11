/**
 * R156.H — WebSocket test-generation helper.
 *
 * Single-source-of-truth for WebSocket connection + message capture
 * in generated PW tests targeting R156.C-detected WebSocket endpoints
 * (`/ws/chat/connect`, `/ws/notifications`, etc.).
 *
 * Why this helper exists: Playwright's `page.on('websocket', ...)`
 * inspects ALL WebSocket lifecycles in a page but doesn't initiate
 * a connection or send/receive messages programmatically. To
 * deterministically test a WebSocket endpoint, we use the browser
 * `WebSocket` constructor inside `page.evaluate` to:
 *   1. Open the connection (supports auth via URL query OR init
 *      message — both common SUT patterns)
 *   2. Collect incoming messages (`type: 'received'`)
 *   3. Optionally send operator-supplied messages (`type: 'sent'`)
 *   4. Stop on timeout OR max-messages cap (operator-bounded;
 *      default 10s timeout + 50 messages)
 *   5. Return the captured message trace for assertion
 *
 * R154 non-mutation guarantee: WebSocket helpers are READ-only by
 * default (open + listen). When operator opts in via `sendMessages`,
 * the helper sends THEIR explicit messages — operator-controlled, not
 * LLM-invented mutations.
 *
 * R156.B token chain: token sourced from `process.env.AUTH_TOKEN`
 * (populated by R95.1 token precedence + R156.J.3 auto-refresh).
 * Operator selects URL-query OR init-message auth pattern via options.
 *
 * Operator-tunable env vars:
 *   ARTA_WS_DEFAULT_TIMEOUT_MS    -- default 10000 (10s per session)
 *   ARTA_WS_DEFAULT_MAX_MESSAGES  -- default 50 messages per session
 *   ARTA_WS_AUTH_QUERY_PARAM      -- default "token" (override for
 *                                     SUTs using `?access_token=...`)
 *
 * @example
 *   import { openAuthenticatedWebSocket, expectWsMessages } from '../common/websocket_helpers';
 *   test('Chat WebSocket emits welcome message', async ({ page }) => {
 *     await page.goto('/chat');
 *     const messages = await openAuthenticatedWebSocket(
 *       page, `wss://${SUT_HOST}/ws/chat/connect`,
 *       { timeoutMs: 8000, maxMessages: 5, authMode: 'query' }
 *     );
 *     await expectWsMessages(messages, m => m.length > 0, 'at least one message');
 *     await expectWsMessages(
 *       messages,
 *       m => m.some(x => x.type === 'received' && x.payload.includes('welcome')),
 *       'welcome message received',
 *     );
 *   });
 */

import type { Page } from '@playwright/test';

export interface WsMessage {
  type: 'sent' | 'received';
  /** Raw payload (string for text frames; JSON-stringified for binary
   *  frames so the operator can assert on shape). */
  payload: string;
  /** Unix timestamp ms when the message was sent or received. */
  timestamp: number;
}

export type WsAuthMode =
  /** Append token to URL as a query param (e.g., `?token=...`). */
  | 'query'
  /** Send token as the first WebSocket message after `onopen`
   *  (JSON-stringified payload that the SUT expects). */
  | 'init_message'
  /** Skip auth injection — useful for SUTs with cookie-based WS auth
   *  via storageState OR for unauthenticated WS endpoints. */
  | 'none';

export interface WsOpenOptions {
  /** Max wallclock to keep the connection open (default 10000ms). */
  timeoutMs?: number;
  /** Max messages (received + sent) to collect before closing. */
  maxMessages?: number;
  /** Auth injection strategy (default 'query'). */
  authMode?: WsAuthMode;
  /** Override the query param name when authMode='query' (default
   *  reads from ARTA_WS_AUTH_QUERY_PARAM env var, falls back to "token"). */
  authQueryParam?: string;
  /** Init-message payload template when authMode='init_message'.
   *  Literal `${token}` substring is replaced with process.env.AUTH_TOKEN.
   *  Default: '{"type":"auth","token":"${token}"}'. */
  initMessageTemplate?: string;
  /** Optional list of messages to send to the SUT after auth handshake.
   *  Each entry is JSON-stringified (if not already a string) and
   *  sent in order. */
  sendMessages?: unknown[];
  /** Optional sub-protocols (WebSocket constructor `protocols` arg). */
  protocols?: string[];
}

/**
 * R156.H — open an authenticated WebSocket connection, capture
 * messages, and return the trace.
 *
 * @param page Playwright page (used to run the WebSocket inside the
 *   browser context so chromium TLS/DNS bridge layers apply).
 * @param url Full WebSocket URL (wss:// or ws://).
 * @param options See WsOpenOptions.
 * @returns Array of WsMessage in chronological order (received +
 *   sent interleaved by timestamp). Empty array when the connection
 *   errors immediately (caller asserts on length).
 */
export async function openAuthenticatedWebSocket(
  page: Page,
  url: string,
  options: WsOpenOptions = {},
): Promise<WsMessage[]> {
  const timeoutMs =
    options.timeoutMs
    ?? Number(process.env.ARTA_WS_DEFAULT_TIMEOUT_MS ?? 10000);
  const maxMessages =
    options.maxMessages
    ?? Number(process.env.ARTA_WS_DEFAULT_MAX_MESSAGES ?? 50);
  const authMode: WsAuthMode = options.authMode ?? 'query';
  const authQueryParam =
    options.authQueryParam
    ?? process.env.ARTA_WS_AUTH_QUERY_PARAM
    ?? 'token';
  const initMessageTemplate =
    options.initMessageTemplate
    ?? '{"type":"auth","token":"${token}"}';
  const token = process.env.AUTH_TOKEN ?? '';
  const sendMessagesStrings = (options.sendMessages ?? []).map((m) =>
    typeof m === 'string' ? m : JSON.stringify(m),
  );
  const protocols = options.protocols ?? [];

  return page.evaluate(
    async (args) => {
      const messages: WsMessage[] = [];
      // Compose URL with token query param when authMode='query'.
      let wsUrl = args.url;
      if (args.authMode === 'query' && args.token) {
        const sep = wsUrl.includes('?') ? '&' : '?';
        wsUrl = `${wsUrl}${sep}${encodeURIComponent(args.authQueryParam)}=${encodeURIComponent(args.token)}`;
      }
      return new Promise<WsMessage[]>((resolve) => {
        let ws: WebSocket;
        try {
          ws = args.protocols.length
            ? new WebSocket(wsUrl, args.protocols)
            : new WebSocket(wsUrl);
        } catch (e) {
          // eslint-disable-next-line no-console
          console.warn(`[R156.H] WS construct exception: ${String(e)}`);
          resolve(messages);
          return;
        }
        const deadline = Date.now() + args.timeoutMs;
        let resolved = false;
        const finalize = () => {
          if (resolved) return;
          resolved = true;
          try {
            if (
              ws.readyState === WebSocket.OPEN
              || ws.readyState === WebSocket.CONNECTING
            ) {
              ws.close();
            }
          } catch {
            /* silent */
          }
          resolve(messages);
        };

        ws.addEventListener('open', () => {
          // After open: inject auth (init_message mode), then send the
          // operator-supplied messages.
          if (args.authMode === 'init_message' && args.token) {
            const payload = args.initMessageTemplate.replace(
              '${token}',
              args.token,
            );
            try {
              ws.send(payload);
              messages.push({
                type: 'sent',
                payload,
                timestamp: Date.now(),
              });
            } catch {
              /* silent */
            }
          }
          for (const m of args.sendMessagesStrings) {
            try {
              ws.send(m);
              messages.push({
                type: 'sent',
                payload: m,
                timestamp: Date.now(),
              });
            } catch {
              /* silent */
            }
            if (messages.length >= args.maxMessages) {
              finalize();
              return;
            }
          }
        });

        ws.addEventListener('message', (ev) => {
          const payload =
            typeof ev.data === 'string'
              ? ev.data
              : (() => {
                  try {
                    return JSON.stringify(ev.data);
                  } catch {
                    return '[binary]';
                  }
                })();
          messages.push({
            type: 'received',
            payload,
            timestamp: Date.now(),
          });
          if (messages.length >= args.maxMessages) {
            finalize();
          }
        });

        ws.addEventListener('error', (ev) => {
          // eslint-disable-next-line no-console
          console.warn(`[R156.H] WS error event: ${String(ev)}`);
        });

        ws.addEventListener('close', () => {
          finalize();
        });

        // Deadline timer — covers SUTs that never close the stream.
        setTimeout(() => {
          if (ws.readyState !== WebSocket.CLOSED) {
            finalize();
          }
        }, Math.max(0, deadline - Date.now()));
      });
    },
    {
      url,
      token,
      authMode,
      authQueryParam,
      initMessageTemplate,
      sendMessagesStrings,
      protocols,
      timeoutMs,
      maxMessages,
    },
  );
}

/**
 * R156.H — predicate-based assertion against captured WS messages.
 *
 * Operator-friendly assertion helper that emits a structured error
 * when the predicate returns false. The `reason` parameter shows up
 * in the test report so operators see WHY the assertion failed.
 */
export async function expectWsMessages(
  messages: WsMessage[],
  predicate: (msgs: WsMessage[]) => boolean,
  reason?: string,
): Promise<void> {
  if (!predicate(messages)) {
    const msgSummary =
      messages.length > 0
        ? messages
            .slice(0, 3)
            .map(
              (m) =>
                `{${m.type}: ${m.payload.slice(0, 40)}${
                  m.payload.length > 40 ? '...' : ''
                }}`,
            )
            .join(', ')
        : '(no messages)';
    throw new Error(
      `[R156.H expectWsMessages] assertion failed: ${reason ?? 'predicate returned false'}. ` +
      `Got ${messages.length} messages; first 3: ${msgSummary}`,
    );
  }
}
