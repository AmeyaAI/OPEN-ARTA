// R156.J.1 — Newman collection-level pre-request script.
//
// Auto-refreshes `auth_token` using `refresh_token` when the agent_token's
// remaining TTL drops below {{REFRESH_THRESHOLD_SECONDS}}.
//
// Runs BEFORE every item in the collection. Read-only with respect to SUT
// business data — the only request fired is a POST to the SUT's own auth
// refresh endpoint (a token-mint operation, not a business mutation).
// R154 non-mutation guarantee: refresh endpoints are exempt from the
// destructive-pattern check per R154.B whitelist (R156.J.3).
//
// Template variables (ARTA's Newman gen path interpolates these at gen
// time, sourced from R156.I.1's source-extracted refresh_flow):
//   {{REFRESH_ENDPOINT}}                -- e.g., https://backend.example.com/api/auth/refresh
//   {{REFRESH_REQUEST_BODY_FIELD}}      -- e.g., "refresh_token" or "refreshToken"
//   {{REFRESH_RESPONSE_ACCESS_FIELD}}   -- e.g., "access_token" or "accessToken"
//   {{REFRESH_RESPONSE_REFRESH_FIELD}}  -- e.g., "refresh_token" (rotation) or "" (no rotation)
//   {{REFRESH_EXPIRES_IN_FIELD}}        -- e.g., "expires_in" or "" (use JWT exp claim only)
//   {{REFRESH_THRESHOLD_SECONDS}}       -- e.g., 60 (refresh when TTL < this many seconds)
//
// SUT without refresh endpoint: R156.I.1 returns refresh_flow=None and ARTA
// SKIPS injecting this script entirely (truthful: long smokes will rely on
// R148-pattern mid-smoke operator paste; not an ARTA gen bug).

const tokenStr =
    pm.environment.get("auth_token")
    || pm.collectionVariables.get("auth_token")
    || pm.globals.get("auth_token")
    || "";
const refreshStr =
    pm.environment.get("refresh_token")
    || pm.collectionVariables.get("refresh_token")
    || pm.globals.get("refresh_token")
    || "";

function _r156_j_1_decode_jwt_exp(token) {
    // Pure-string JWT exp claim decoder. No external deps — Newman pm
    // runtime has only atob/btoa available. Returns Unix-seconds or null.
    try {
        const parts = String(token).split(".");
        if (parts.length !== 3) return null;
        // Base64URL → standard base64 (Newman atob requires standard alphabet)
        const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
        const padded = b64 + "===".slice((b64.length + 3) % 4);
        const payloadStr = (typeof atob === "function") ? atob(padded) : Buffer.from(padded, "base64").toString();
        const payload = JSON.parse(payloadStr);
        return typeof payload.exp === "number" ? payload.exp : null;
    } catch (e) {
        return null;
    }
}

const _r156_j_now_sec = Math.floor(Date.now() / 1000);
const _r156_j_exp_sec = _r156_j_1_decode_jwt_exp(tokenStr);
const _r156_j_ttl_remaining = _r156_j_exp_sec ? (_r156_j_exp_sec - _r156_j_now_sec) : null;

if (
    _r156_j_ttl_remaining !== null
    && _r156_j_ttl_remaining < {{REFRESH_THRESHOLD_SECONDS}}
    && refreshStr
    && "{{REFRESH_ENDPOINT}}"
) {
    console.log(
        "[R156.J.1] auth_token TTL=" + _r156_j_ttl_remaining
        + "s (< {{REFRESH_THRESHOLD_SECONDS}}s); refreshing via {{REFRESH_ENDPOINT}}"
    );
    const _r156_j_body = {};
    _r156_j_body["{{REFRESH_REQUEST_BODY_FIELD}}"] = refreshStr;
    pm.sendRequest(
        {
            url: "{{REFRESH_ENDPOINT}}",
            method: "POST",
            header: { "Content-Type": "application/json", "Accept": "application/json" },
            body: { mode: "raw", raw: JSON.stringify(_r156_j_body) },
        },
        function (err, response) {
            if (err) {
                console.warn("[R156.J.1] refresh request errored: " + err + "; proceeding with stale token");
                return;
            }
            if (!response || response.code >= 400) {
                console.warn(
                    "[R156.J.1] refresh failed (code=" + (response ? response.code : "no_response")
                    + "); proceeding with stale token"
                );
                return;
            }
            let body;
            try {
                body = response.json();
            } catch (e) {
                console.warn("[R156.J.1] refresh response not JSON-parsable; proceeding with stale token");
                return;
            }
            const newAccess = body["{{REFRESH_RESPONSE_ACCESS_FIELD}}"];
            if (newAccess) {
                pm.environment.set("auth_token", newAccess);
                pm.collectionVariables.set("auth_token", newAccess);
                pm.globals.set("auth_token", newAccess);
                const _newExp = _r156_j_1_decode_jwt_exp(newAccess);
                const _newTtl = _newExp ? (_newExp - _r156_j_now_sec) : null;
                console.log(
                    "[R156.J.1] auth_token refreshed; new TTL="
                    + (_newTtl !== null ? _newTtl + "s" : "unknown (no exp claim)")
                );
            } else {
                console.warn(
                    "[R156.J.1] refresh response missing '{{REFRESH_RESPONSE_ACCESS_FIELD}}' field; "
                    + "proceeding with stale token"
                );
                return;
            }
            // Rotation: when the SUT issues a new refresh_token, persist it
            // so the NEXT refresh cycle (mid-smoke) uses the fresh one.
            const _R156_J_REFRESH_FIELD = "{{REFRESH_RESPONSE_REFRESH_FIELD}}";
            if (_R156_J_REFRESH_FIELD) {
                const newRefresh = body[_R156_J_REFRESH_FIELD];
                if (newRefresh) {
                    pm.environment.set("refresh_token", newRefresh);
                    pm.collectionVariables.set("refresh_token", newRefresh);
                    pm.globals.set("refresh_token", newRefresh);
                    console.log("[R156.J.1] refresh_token rotated");
                }
            }
        }
    );
} else if (_r156_j_ttl_remaining === null && tokenStr) {
    // Opaque token (no JWT exp claim). Cannot preemptively refresh.
    // SUT relies on 401-then-retry pattern (operator can set
    // ARTA_REFRESH_THRESHOLD_SEC=0 to disable preemptive refresh; future
    // R157 candidate: 401-driven retry-with-refresh wrapper).
    // (silent; no log noise per Newman item)
}
