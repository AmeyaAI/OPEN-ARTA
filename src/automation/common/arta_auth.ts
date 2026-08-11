/**
 * R207 — per-path auth for API-contract specs.
 *
 * `Bearer <agent_api_token>`, and the collection-manager default needs
 *
 * Pre-R207 the generated specs hard-coded `Bearer ${process.env.AUTH_TOKEN}`
 * (the agent token), so every collection-manager API-contract assertion hit a
 * 500 instead of the real 200/40x status (run-cf956e: 102 such FAILs).
 *
 * This is a GENERIC applier — the auth RULES live in Python
 * truth) and are injected at dispatch as `ARTA_AUTH_CHAIN` (JSON rules) +
 * SUT-specific logic lives here, so adding a new family is a Python-only change.
 *
 * Usage in a generated spec:
 *   import { authHeaderFor } from '../common/arta_auth';
 *   const resp = await request.get(apiBase + path, { headers: authHeaderFor(path) });
 */

type AuthRule = {
  match: string;
  scheme?: string;        // 'bearer' | 'cookie' | 'none'
  value_template?: string;
  cookie_name?: string;
  host?: string;          // family key into the host map (R213.J: one rule → host+auth)
  // A1 (R218) — additive cookies sent ALONGSIDE the primary scheme (composite
  cookies?: { name: string; value_template?: string }[];
};

function _loadChain(): AuthRule[] {
  try {
    const v = JSON.parse(process.env.ARTA_AUTH_CHAIN || '[]');
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

function _loadTokens(): Record<string, string> {
  try {
    const v = JSON.parse(process.env.ARTA_AUTH_TOKENS || '{}');
    return v && typeof v === 'object' ? v : {};
  } catch {
    return {};
  }
}

function _matches(rule: AuthRule, path: string): boolean {
  // Mirrors AuthRule.matches: '*' is catch-all, else case-insensitive substring.
  if (!rule.match || rule.match === '*') return true;
  return (path || '').toLowerCase().includes(rule.match.toLowerCase());
}

// R213.J — specificity score [tier, length] mirroring Python AuthRule.specificity:
// tier 2 = anchored segment-aligned path-prefix, tier 1 = mid-path substring,
// tier 0 = '*'. null when no match. Higher wins (tier, then length).
function _specificity(rule: AuthRule, path: string): [number, number] | null {
  if (rule.match === '*') return [0, 0];
  const m = (rule.match || '').toLowerCase();
  const p = (path || '').toLowerCase();
  if (!m || p.indexOf(m) < 0) return null;
  if (m.charAt(0) === '/' && p.indexOf(m) === 0) {
    const nx = p.charAt(m.length);
    if (nx === '' || nx === '/' || nx === '?') return [2, m.length];
  }
  return [1, m.length];
}

// R213.J — most-specific matching rule, order-independent (parity with Python
// AuthChain.best_rule). Killswitch ARTA_AUTH_SPECIFICITY_DISABLE=1 → first-match.
function _bestRule(chain: AuthRule[], path: string): AuthRule | null {
  if (process.env.ARTA_AUTH_SPECIFICITY_DISABLE === '1') {
    for (const rule of chain) if (_matches(rule, path)) return rule;
    return null;
  }
  let best: AuthRule | null = null;
  let bs: [number, number] | null = null;
  for (const rule of chain) {
    const sc = _specificity(rule, path);
    if (sc === null) continue;
    if (bs === null || sc[0] > bs[0] || (sc[0] === bs[0] && sc[1] > bs[1])) {
      best = rule;
      bs = sc;
    }
  }
  return best;
}

function _interpolate(tpl: string, tokens: Record<string, string>): string | null {
  // Mirrors auth_chain._interpolate: returns null when ANY placeholder is
  let out = tpl;
  const names = (tpl.match(/\{(\w+)\}/g) || []).map((s) => s.slice(1, -1));
  for (const n of names) {
    const val = tokens[n];
    if (!val) return null;
    out = out.split('{' + n + '}').join(val);
  }
  return out;
}

/**
 * Resolve the Authorization (or Cookie) header(s) for `path`. Falls back to a
 * or a rule's token is missing — never ships a broken/half-resolved header.
 */
export function authHeaderFor(path: string): Record<string, string> {
  const chain = _loadChain();
  const tokens = _loadTokens();
  const fallbackTok = tokens.session_token || process.env.AUTH_TOKEN || '';
  const fallback: Record<string, string> = fallbackTok
    ? { Authorization: 'Bearer ' + fallbackTok }
    : {};
  const rule = _bestRule(chain, path);   // R213.J — most-specific, order-independent
  if (!rule) return fallback;
  // A1 (R218) — resolve the rule's additive cookies (composite auth). A missing
  // additive cookie token marks the composite unresolved (don't ship a half-auth).
  const cookieParts: string[] = [];
  let cookiesOk = true;
  for (const c of rule.cookies || []) {
    const cv = _interpolate(c.value_template || '{session_token}', tokens);
    if (cv) cookieParts.push(c.name + '=' + cv);
    else cookiesOk = false;
  }
  const withCookies = (h: Record<string, string>): Record<string, string> =>
    cookieParts.length ? { ...h, Cookie: cookieParts.join('; ') } : h;
  if (rule.scheme === 'none') return cookiesOk ? withCookies({}) : fallback;
  if (rule.scheme === 'cookie') {
    const cv = tokens.session_token || tokens.cookie_value;
    return (cv && cookiesOk)
      ? withCookies({ Cookie: (rule.cookie_name || 'session-token') + '=' + cv })
      : fallback;
  }
  const val = _interpolate(rule.value_template || '{session_token}', tokens);
  return (val && cookiesOk) ? withCookies({ Authorization: 'Bearer ' + val }) : fallback;
}

// ── R210 — multi-host: route a path to the right family host ─────────────────
function _loadHostMap(): Record<string, string> {
  try {
    const v = JSON.parse(process.env.ARTA_HOST_MAP || '{}');
    return v && typeof v === 'object' ? v : {};
  } catch {
    return {};
  }
}

/**
 * R210 — return the FULL URL for `path`, routed to the correct per-family host
 * (the SUT is multi-host: auth / analytics / extraction / monitoring on
 * separate hosts from the collection-manager backend). The host map is injected
 * at dispatch (`ARTA_HOST_MAP`, built from `sut_topology.build_host_map`). When
 * the path carries no family keyword (the common collection-manager case) it
 * falls back to the `backend` host, then `process.env.API_BASE_URL` — so
 * single-host SUTs behave exactly as before.
 */
export function apiUrlFor(path: string): string {
  const hostMap = _loadHostMap();
  const apiBase = (process.env.API_BASE_URL || process.env.BASE_URL || '').replace(/\/$/, '');
  // R213.J — host from the SAME most-specific auth-chain rule (so host + auth
  // come from one match). Falls back to the keyword scan, then backend/apiBase.
  const rule = _bestRule(_loadChain(), path);
  if (rule && rule.host && hostMap[rule.host]) {
    return hostMap[rule.host].replace(/\/$/, '') + path;
  }
  const lower = (path || '').toLowerCase();
  for (const fam of Object.keys(hostMap)) {
    if (!fam || fam === 'backend' || fam === 'api') continue;
    if (lower.includes('/' + fam) || lower.includes(fam)) {
      return hostMap[fam].replace(/\/$/, '') + path;
    }
  }
  const base = (hostMap['backend'] || hostMap['api'] || apiBase || '').replace(/\/$/, '');
  return (base || apiBase) + path;
}
