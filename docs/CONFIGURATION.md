# Configuration reference

## deploy/config.env (used by `deploy/deploy.sh`)
Bash-style `KEY="value"` lines; keep JSON in single quotes. The file holds secrets: `chmod 600`, never commit it.

### Kubernetes and exposure
| Setting | Default | Description |
|---|---|---|
| `KUBE_CONTEXT` | current context | kubectl context to deploy to |
| `NAMESPACE` | `aigw-budget-admin` | Namespace (created if missing, labelled Pod Security `restricted`) |
| `PUBLIC_URL` | — (required) | Exact URL admins open, `https://host[:port]`, no path. Sets the OIDC redirect URI `<PUBLIC_URL>/oauth2/callback` and Secure cookies when https |
| `EXPOSE` | — (required) | `ingress` (Ingress + ClusterIP Service), `loadbalancer` (LoadBalancer Service), `clusterip` (port-forward) |
| `INGRESS_CLASS` | `nginx` | `ingressClassName` |
| `INGRESS_TLS_SECRET` | empty | Existing TLS secret in `NAMESPACE` for the host; empty = no TLS block (TLS terminated elsewhere) |
| `LB_IP` | empty | Fixed LoadBalancer IP via the MetalLB annotation; other LBs: leave empty and edit `deploy/templates/20-service.yaml` for provider annotations |
| `APP_IMAGE` | `python:3.12-slim` | Image that runs the app (files come from a ConfigMap). Or your own build of the `Dockerfile` |
| `PROXY_IMAGE` | `quay.io/oauth2-proxy/oauth2-proxy:v7.7.1` | oauth2-proxy image |

### Prisma AIRS AI Gateway
| Setting | Default | Description |
|---|---|---|
| `SCM_TSG_ID` | — (required) | Tenant service group ID |
| `SCM_CLIENT_ID` / `SCM_CLIENT_SECRET` | — (required) | SCM service account (stored in Secret `aigw-budget-admin-scm`) |
| `DEPARTMENTS` | — (required) | JSON list of workspaces: `[{"name": "<display name>", "workspace_id": "<UUID>", "slug": "<ws-…>"}]`. Get them with `tools/list_workspaces.py`. The API needs the **UUID**; the logs need the **slug** |

### Behaviour
| Setting | Default | Description |
|---|---|---|
| `DEMO` | `false` | `true`: synthetic data, no service account needed (preview / test sign-in) |
| `TIER_KEY` | `metadata.jwt_tier` | Metadata key used in your tier policies' conditions. Drives the tier badge and which policy is shown as a user's primary budget |
| `SPEND_WINDOW_HOURS` | `168` | Window for spend numbers from request logs (168 = 7 days) |
| `READ_ONLY` | `true` | `true`: Reset hidden and `/api/reset` refused. `false`: resets allowed (writes to the tenant) |
| `TITLE_SUFFIX` | empty | Appended to the browser title, e.g. ` (Acme)` |
| `APP_SUBTITLE` | `Department and per-user LLM budgets` | Header subtitle |
| `CACHE_SECONDS` | `30` | How often the snapshot is rebuilt from the API |

### Admin sign-in (oauth2-proxy)
| Setting | Default | Description |
|---|---|---|
| `OIDC_ISSUER_URL` | — (required) | IdP issuer (discovery at `<issuer>/.well-known/openid-configuration`) |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | — (required) | Confidential web app (Secret `aigw-budget-admin-oidc`) |
| `OIDC_PROVIDER` | `oidc` | `oidc` or `keycloak-oidc` |
| `OIDC_EMAIL_CLAIM` | `email` | Claim holding the user's email (Entra: `email` with the optional claim, or `preferred_username`) |
| `OIDC_SCOPE` | `openid email profile` | Requested scopes |
| `OIDC_ALLOW_UNVERIFIED_EMAIL` | `false` | `true` if the IdP omits `email_verified` (Entra, many Keycloak realms) |
| `ADMIN_EMAILS` | — (required) | Comma-separated emails allowed in; everyone else gets "This account isn't an approved admin" |
| `IDP_LOGOUT_URL` | empty | IdP end-session URL; Sign out redirects there (its host is allowlisted in oauth2-proxy). Empty = sign out of the dashboard only |
| `COOKIE_SECRET` | — (required) | 16/24/32 random bytes, e.g. 32 bytes base64 (44 chars). Changing it signs everyone out |

## App environment variables (`app/server.py`)
Set by the deployment from the values above; useful when running the app directly.

| Variable | Default | Description |
|---|---|---|
| `LISTEN` | `127.0.0.1:8080` | Bind address. Keep it on localhost in Kubernetes (only oauth2-proxy should reach it) |
| `DEMO` | `false` | Synthetic data |
| `SCM_TSG_ID`, `SCM_CLIENT_ID`, `SCM_CLIENT_SECRET` | — | Service account (not needed with `DEMO=true`) |
| `DEPARTMENTS` | — (demo: built-in) | Workspaces JSON (optional `group` key = Keycloak group, roster mode) |
| `SPEND_SOURCE` | `logs` | `logs` (AI Gateway request logs API) or `jaeger` (gateway OpenTelemetry traces in Jaeger: `JAEGER_URL`, `JAEGER_SERVICE`) |
| `SPEND_WINDOW_HOURS` | `168` | Spend window |
| `TIER_KEY` | `metadata.jwt_tier` | Tier condition key |
| `READ_ONLY` | `true` | Disable resets |
| `CACHE_SECONDS` | `30` | Snapshot refresh |
| `TITLE_SUFFIX`, `APP_SUBTITLE`, `SIGNOUT_URL` | see above | Page branding and sign-out link |
| `AIGW_API_BASE` | `https://api.apps.paloaltonetworks.com/ai_gw/v2` | Management API base |
| `SCM_TOKEN_URL` | `https://auth.apps.paloaltonetworks.com/oauth2/access_token` | Token endpoint |
| `KC_URL`, `KC_REALM`, `KC_CLIENT_ID`, `KC_CLIENT_SECRET` | empty | Optional Keycloak roster: lists group members (department = `group`) with names and a `user_tier` attribute, including users with no spend yet |
