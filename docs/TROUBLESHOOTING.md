# Troubleshooting

Start with the logs:
```bash
kubectl -n aigw-budget-admin get pods
kubectl -n aigw-budget-admin logs deploy/aigw-budget-admin -c app --tail=50
kubectl -n aigw-budget-admin logs deploy/aigw-budget-admin -c oauth2-proxy --tail=50
```

## Deployment
| Symptom | Fix |
|---|---|
| `config error: …` from `deploy.sh` | Fix the named setting in `config.env` (see [CONFIGURATION.md](CONFIGURATION.md)) |
| `workspace_id must be the workspace UUID, not the slug` | Use the `id` from `tools/list_workspaces.py`, and the `ws-…` value as `slug` |
| Pod `CreateContainerConfigError` / `ImagePullBackOff` | Images not reachable: mirror them and set `APP_IMAGE` / `PROXY_IMAGE` |
| Pod rejected by Pod Security | A cluster policy stricter than `restricted`, or a mutating webhook adding privileges; check events (`kubectl -n … describe pod`) |
| App container exits with `config error: …` | Settings ConfigMap incomplete; re-run `deploy.sh` |

## Sign-in
| Symptom | Fix |
|---|---|
| IdP error "redirect_uri mismatch" / "invalid redirect" | Register exactly `<PUBLIC_URL>/oauth2/callback` in the IdP app (scheme, host, port) |
| oauth2-proxy log `email in id_token (…) isn't verified` | Set `OIDC_ALLOW_UNVERIFIED_EMAIL="true"` (Entra, Keycloak without verified emails) |
| "This account isn't an approved admin" after sign-in | The email isn't in `ADMIN_EMAILS` (case and alias must match what the IdP sends in `OIDC_EMAIL_CLAIM`) |
| Entra: user has no email / wrong value | Add the optional `email` claim, or set `OIDC_EMAIL_CLAIM="preferred_username"` |
| Keycloak: `audience claims [aud] do not exist in claims` | Add an Audience mapper with the client ID (DEPLOYMENT.md step 6) |
| Login loop | `PUBLIC_URL` doesn't match the URL in the browser, or http with `cookie-secure`: use https or set `PUBLIC_URL` to the http URL |
| `unable to obtain CSRF cookie` | Browser blocked the cookie (different host than `PUBLIC_URL`); open the exact `PUBLIC_URL` |
| Sign out goes to an error page | `IDP_LOGOUT_URL` wrong, or the post-logout URI isn't registered in the IdP; leave it empty to sign out of the dashboard only |

## Service account
| Symptom | Fix |
|---|---|
| `SCM token failed: 401` | Wrong client ID/secret or TSG ID |
| `SCM token failed` with timeout | No egress to `auth.apps.paloaltonetworks.com:443` (firewall / proxy) |
| Workspace card shows an error code (403) | Role lacks access to that workspace, or the UUID is wrong |
| Reset fails with "gateway returned 403" | Role can read but not write budget policies; grant it, or keep `READ_ONLY="true"` |

## Data
| Symptom | Fix |
|---|---|
| No users in a workspace | No requests in the last `SPEND_WINDOW_HOURS`; users appear after their first request |
| Tier badge empty | Requests don't carry `TIER_KEY` metadata. For JWT auth, extract the tier claim (`jwt_` prefix) and check `TIER_KEY` matches the policy condition key |
| User has no budget meter | No per-user policy matches the user (policies must be grouped by `metadata._user`; the condition must match their tier) |
| Budget counter lower than spend | Normal: counters include only matching requests since the policy was created / reset ([HOW-IT-WORKS.md](HOW-IT-WORKS.md#numbers)) |
| Workspace budget "No cap" | The workspace has no Budget Allocation in SCM |
| Spend looks 100× too high | You're comparing log cents with dollars elsewhere; the app converts cents to dollars |
| Numbers lag | Snapshot is rebuilt every `CACHE_SECONDS`; click **Refresh** |
