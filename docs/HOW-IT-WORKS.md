# How it works

## Components
| Component | Role |
|---|---|
| **oauth2-proxy** (container, port 4180) | OIDC sign-in with your IdP (authorization code + PKCE), email allowlist, session cookie. Passes `X-Forwarded-Email` to the app |
| **app** (`server.py`, container, `127.0.0.1:8080`) | Calls the Prisma AIRS management API, builds a snapshot every `CACHE_SECONDS`, serves the UI and a JSON API. Python standard library only |
| **UI** (`index.html`) | Single page, no external assets. Polls `/api/overview` every 30 s |

Both containers are in one pod; the app is reachable only through oauth2-proxy.

## API calls
Base `https://api.apps.paloaltonetworks.com/ai_gw/v2`, header `Authorization: Bearer <SCM token>`.

| Purpose | Call |
|---|---|
| Token (cached until expiry) | `POST https://auth.apps.paloaltonetworks.com/oauth2/access_token` (Basic auth with client ID/secret, `grant_type=client_credentials`, `scope=tsg_id:<TSG>`) |
| Workspace budget allocation | `GET /workspaces/{workspace_uuid}` → `usage_limits[]` (`credit_limit`, `current_usage`, `periodic_reset` or `periodic_reset_days`, `alert_threshold`) |
| Budget policies | `GET /policies/usage-limits?workspace_id={uuid}`, then `GET /policies/usage-limits/{id}?workspace_id={uuid}` for each (the list omits `conditions` and `group_by`) |
| Per-user counters | `GET /policies/usage-limits/{id}/entities?workspace_id={uuid}&page_size=100` → one entity per user (`value_key` `metadata._user:<user>`, `current_usage`) |
| Reset (when enabled) | `PUT /policies/usage-limits/{id}/entities/{entity_id}/reset?workspace_id={uuid}` |
| Spend per request | `GET /logs?organisationId={TSG}&workspaceSlug={slug}&timeOfGenerationMin=…&timeOfGenerationMax=…&pageSize=500&currentPage=N` → records with `_user`, `ai_model`, `cost` (`cost_unit: cents`), `created_at`, metadata (`metadataKey`/`metadataValue`) |

## Numbers
| Shown | Source |
|---|---|
| Workspace budget "used of limit" | `current_usage` of the workspace budget allocation (the gateway's counter for the current period). If the API doesn't return it, the sum of logged spend since the period start |
| User budget meter | The user's entity `current_usage` under the policy (the gateway's counter, the one that enforces the limit) |
| Primary budget | The policy whose `TIER_KEY` condition matches the user's tier; otherwise the one closest to its limit |
| Tier badge | The user's latest `TIER_KEY` metadata value in the logs (or the Keycloak `user_tier` attribute in roster mode) |
| Spend by model / user / workspace total | Sum of logged `cost` over `SPEND_WINDOW_HOURS` (cents → dollars); blocked/failed requests have cost 0 |
| Users over budget | Users whose counter ≥ limit on any policy |

Why a counter can differ from logged spend: counters count only requests that matched the policy (e.g. carried the
tier metadata) since the policy was created or last reset; logged spend counts every request in the window.

Only budget policies grouped by `metadata._user` are shown as per-user budgets. Workspace-wide caps should use the
workspace Budget Allocation.

## JSON API (behind the sign-in)
| Endpoint | |
|---|---|
| `GET /api/overview` (`?refresh=1` forces a rebuild) | Snapshot: totals, workspaces (budget, policies, users, models), `read_only`, `demo`, signed-in `actor` |
| `POST /api/reset` | Body `{"workspace_id","policy_id","entity_id","value_key"}`, `Content-Type: application/json` required. 403 when read-only |
| `GET /api/audit` | Recent resets (in memory, max 200) |
| `GET /healthz` | Liveness |

## Security model
- **Authentication / authorization:** oauth2-proxy with your IdP; only `ADMIN_EMAILS` get a session. Session cookie
  HttpOnly, SameSite=Lax, Secure when `PUBLIC_URL` is https, 8 h.
- **App exposure:** the app binds to 127.0.0.1 inside the pod; nothing else can reach it.
- **CSRF:** `/api/reset` requires `Content-Type: application/json` (a cross-site form can't send it without a CORS
  preflight, which the app doesn't allow) plus the SameSite session cookie.
- **Headers:** `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- **Secrets:** SCM and OIDC secrets live only in Kubernetes Secrets (rendered in memory by `deploy.sh` and piped to
  kubectl; `--dry-run` redacts them). Nothing is written to disk by the tooling except your own `config.env`.
- **Pod:** non-root (65532), read-only root file system, all capabilities dropped, seccomp RuntimeDefault, no service
  account token, namespace Pod Security `restricted`.
- **Least privilege:** keep `READ_ONLY="true"` unless resets are needed; give the service account only the AI Gateway
  access it needs.
- **Audit:** every reset is logged (`AUDIT {...}` with admin email, workspace, policy, user, result) to stdout.

## Limits
- One replica (cache and in-app audit list are in memory).
- Users appear after their first logged request (unless you use the Keycloak roster).
- Large tenants: logs are paged 500 per call, up to 10,500 requests per workspace per window; raise
  `SPEND_WINDOW_HOURS` carefully.
