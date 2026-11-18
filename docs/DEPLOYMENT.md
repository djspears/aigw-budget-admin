# Deployment walkthrough

This guide takes you from nothing to a running dashboard, signed in through your identity provider. Allow about an
hour the first time (most of it is creating the service account and the IdP app).

**Contents**
0. [Preview with demo data (optional)](#0-preview-with-demo-data-optional)
1. [Plan](#1-plan)
2. [Prerequisites checklist](#2-prerequisites-checklist)
3. [Create the SCM service account](#3-create-the-scm-service-account)
4. [Verify access and pick the workspaces](#4-verify-access-and-pick-the-workspaces)
5. [Check the budget setup in AI Gateway](#5-check-the-budget-setup-in-ai-gateway)
6. [Create the OIDC app in your identity provider](#6-create-the-oidc-app-in-your-identity-provider)
7. [DNS and TLS](#7-dns-and-tls)
8. [Fill in config.env](#8-fill-in-configenv)
9. [Review the manifests](#9-review-the-manifests)
10. [Deploy](#10-deploy)
11. [Verify](#11-verify)
12. [Enable budget resets (optional)](#12-enable-budget-resets-optional)
13. [Day 2: updates, secrets, uninstall](#13-day-2-updates-secrets-uninstall)

---

## 0. Preview with demo data (optional)
```bash
git clone https://github.com/djspears/aigw-budget-admin.git && cd aigw-budget-admin
DEMO=true READ_ONLY=false python3 app/server.py
```
Open http://127.0.0.1:8080. Everything works against synthetic data (including Reset). Stop with Ctrl+C.
You can also deploy to the cluster with `DEMO="true"` in `config.env` to test sign-in and networking before the
service account exists (steps 6–10).

## 1. Plan
Decide:
| Decision | Options | Recommendation |
|---|---|---|
| URL | e.g. `https://ai-budgets.example.com` | A dedicated hostname |
| Exposure | `ingress` (TLS at your ingress controller), `loadbalancer`, `clusterip` (port-forward only) | `ingress` with TLS |
| Identity provider | Okta, Microsoft Entra ID, Keycloak, Ping, Auth0, … (any OIDC) | Your corporate IdP |
| Admins | Email addresses allowed to sign in | Small group (FinOps / AI platform team) |
| Workspaces | Which AI Gateway workspaces to show | Those with budgets |
| Reset | Read-only or allow resets | Start read-only (`READ_ONLY="true"`), enable later |

## 2. Prerequisites checklist
- [ ] **Prisma AIRS AI Gateway** in Strata Cloud Manager with the workspaces you want to monitor.
- [ ] **SCM admin** who can create a service account and assign a role (step 3).
- [ ] **Kubernetes** 1.25+ cluster; `kubectl` access that can create a namespace, Secrets, ConfigMaps, a Deployment,
      a Service and an Ingress. The namespace is labelled Pod Security `restricted` (the pod complies).
- [ ] **Egress** from the cluster (HTTPS 443) to `auth.apps.paloaltonetworks.com`, `api.apps.paloaltonetworks.com`
      and your IdP's issuer host. If you use an egress proxy/firewall, allow these.
- [ ] **Images** pullable: `python:3.12-slim` and `quay.io/oauth2-proxy/oauth2-proxy:v7.7.1` (or mirror them and set
      `APP_IMAGE` / `PROXY_IMAGE`).
- [ ] **Ingress controller** (e.g. ingress-nginx) and a **TLS certificate** for the hostname (cert-manager or an
      existing secret), or a LoadBalancer implementation.
- [ ] **IdP admin** who can create an OIDC web application (step 6).
- [ ] **Workstation**: `bash`, `python3` 3.8+, `kubectl`, `git`.

```bash
for t in bash python3 kubectl git; do command -v $t >/dev/null && echo "ok  $t" || echo "MISSING  $t"; done
kubectl auth can-i create namespace && kubectl auth can-i create deployments -A
```

## 3. Create the SCM service account
1. In **Strata Cloud Manager → Common Services (Settings) → Identity & Access → Access Management**, select your tenant
   (TSG) and **Add Identity → Service Account**. Name it e.g. `aigw-budget-admin`.
2. Copy the **Client ID** (`<name>@<TSG>.iam.panserviceaccount.com`) and **Client Secret** now; the secret is shown once.
3. **Assign a role** on the tenant (or the AI Gateway workspace scopes) that can read AI Gateway workspaces, budget
   (usage-limit) policies and request logs, and, if you will enable Reset, reset budget counters. A tenant-wide
   superuser role works; for least privilege use a custom role limited to AI Gateway access. Step 4 verifies the
   read access; step 12 verifies reset.
4. Note the **TSG ID** (tenant service group ID, numeric).

Store the secret in your secret manager. It goes only into `deploy/config.env` (git-ignored) and then a Kubernetes
Secret.

## 4. Verify access and pick the workspaces
```bash
export SCM_TSG_ID="<tsg-id>" SCM_CLIENT_ID="<client-id>" SCM_CLIENT_SECRET="<client-secret>"
python3 tools/list_workspaces.py
```
Expected: `SCM token: OK`, then each workspace with its UUID, slug, budget allocation and budget policies, and finally:
```
DEPARTMENTS='[{"name":"Engineering","workspace_id":"7c3f...","slug":"ws-engine-9749f1"}, ...]'
```
Keep the workspaces you want (edit the `name` values to friendly display names if you like). If the token or the
workspace call fails, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#service-account).

## 5. Check the budget setup in AI Gateway
The dashboard reads what is configured in SCM; it does not create budgets.

| Dashboard element | Needs in AI Gateway |
|---|---|
| Workspace budget meter | Workspace **Budget Allocation** (cost limit, optional weekly/monthly reset) |
| Per-user budget + Reset | **Budget (usage-limit) policies** grouped by **`metadata._user`** |
| Tier badge and "which policy applies" | Those policies have a **condition on a tier metadata key**, e.g. `metadata.jwt_tier` = `t1`; requests carry that metadata (for JWT auth: claim `tier` extracted with the `jwt_` prefix → `metadata.jwt_tier`) |
| Spend by model / user | Request logging on (default) |

In the `list_workspaces.py` output, check that the tier policies show `group_by=['metadata._user']` and note the
condition key: it is your `TIER_KEY`. If a workspace shows no policies, the dashboard still shows its budget
allocation and spend.

## 6. Create the OIDC app in your identity provider
Values you need for any IdP:
| Setting | Value |
|---|---|
| App type | Web application, **confidential** (client secret), authorization code flow (+ PKCE S256) |
| Redirect (sign-in) URI | `<PUBLIC_URL>/oauth2/callback`, e.g. `https://ai-budgets.example.com/oauth2/callback` |
| Post-logout redirect URI | `<PUBLIC_URL>/` |
| Scopes | `openid email profile` |
| Users | Assign the admins (the app also enforces `ADMIN_EMAILS`) |
| Collect | Issuer URL, client ID, client secret |

<details open><summary><b>Okta</b></summary>

1. **Applications → Create App Integration → OIDC – OpenID Connect → Web Application.**
2. Grant type **Authorization Code**. Sign-in redirect URI `<PUBLIC_URL>/oauth2/callback`; sign-out redirect URI `<PUBLIC_URL>/`.
3. **Assignments:** the admin users or group. Save; copy **Client ID** and **Client secret**.
4. Issuer: `https://<your-org>.okta.com` (org authorization server) or `https://<your-org>.okta.com/oauth2/default`.
5. config.env: `OIDC_PROVIDER="oidc"`, `OIDC_EMAIL_CLAIM="email"`. Leave `IDP_LOGOUT_URL` empty (Okta's logout endpoint needs an ID token hint), so Sign out ends the dashboard session.
</details>

<details><summary><b>Microsoft Entra ID</b></summary>

1. **Entra admin center → App registrations → New registration.** Redirect URI: platform **Web**, `<PUBLIC_URL>/oauth2/callback`.
2. **Certificates & secrets → New client secret**; copy the value. Copy the **Application (client) ID** and **Directory (tenant) ID**.
3. **Token configuration → Add optional claim → ID token → `email`** (or use `preferred_username`).
4. Optional: **Enterprise applications → the app → Properties → Assignment required = Yes**, then assign the admins.
5. config.env:
   ```
   OIDC_ISSUER_URL="https://login.microsoftonline.com/<tenant-id>/v2.0"
   OIDC_PROVIDER="oidc"
   OIDC_EMAIL_CLAIM="email"                 # or "preferred_username"
   OIDC_ALLOW_UNVERIFIED_EMAIL="true"       # Entra doesn't send email_verified
   IDP_LOGOUT_URL="https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/logout?post_logout_redirect_uri=<url-encoded PUBLIC_URL>/"
   ```
</details>

<details><summary><b>Keycloak</b></summary>

1. **Clients → Create client**: type OpenID Connect, client ID `aigw-budget-admin`; **Client authentication ON**;
   **Standard flow** only. Valid redirect URIs `<PUBLIC_URL>/oauth2/callback`; valid post-logout redirect URIs `<PUBLIC_URL>/*`.
2. **Advanced → Proof Key for Code Exchange: S256.** **Credentials** tab: copy the client secret.
3. **Client scopes → aigw-budget-admin-dedicated → Add mapper → Audience** with included client audience = the client ID
   (oauth2-proxy checks the `aud` claim).
4. config.env:
   ```
   OIDC_ISSUER_URL="https://<keycloak-host>/realms/<realm>"
   OIDC_PROVIDER="keycloak-oidc"
   OIDC_ALLOW_UNVERIFIED_EMAIL="true"       # unless your users' emails are verified
   IDP_LOGOUT_URL="https://<keycloak-host>/realms/<realm>/protocol/openid-connect/logout?client_id=aigw-budget-admin&post_logout_redirect_uri=<url-encoded PUBLIC_URL>%2F"
   ```
</details>

Other OIDC providers: use `OIDC_PROVIDER="oidc"` with their issuer URL; set `OIDC_EMAIL_CLAIM` to the claim that holds
the user's email.

## 7. DNS and TLS
- **Ingress (recommended):** create a DNS record for the hostname pointing at your ingress controller. Create the TLS
  secret in the app's namespace (name = `INGRESS_TLS_SECRET`), e.g. with cert-manager or:
  ```bash
  kubectl create namespace aigw-budget-admin
  kubectl -n aigw-budget-admin create secret tls budgets-tls --cert=fullchain.pem --key=privkey.pem
  ```
  (`deploy.sh` leaves an existing namespace and secret in place.)
- **LoadBalancer:** the Service gets an external IP (`LB_IP` pins it on MetalLB). Use `PUBLIC_URL="http://<ip>"` only
  on trusted networks; cookies are not marked Secure without https.
- **ClusterIP:** reach it with `kubectl -n aigw-budget-admin port-forward svc/aigw-budget-admin 8080:80` and
  `PUBLIC_URL="http://localhost:8080"` (register that redirect URI in the IdP).

## 8. Fill in config.env
```bash
cp deploy/config.env.example deploy/config.env
chmod 600 deploy/config.env
python3 -c 'import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'   # COOKIE_SECRET
```
Edit `deploy/config.env`. Minimum:
```bash
KUBE_CONTEXT="prod-cluster"
NAMESPACE="aigw-budget-admin"
PUBLIC_URL="https://ai-budgets.example.com"
EXPOSE="ingress"
INGRESS_CLASS="nginx"
INGRESS_TLS_SECRET="budgets-tls"

SCM_TSG_ID="1234567890"
SCM_CLIENT_ID="aigw-budget-admin@1234567890.iam.panserviceaccount.com"
SCM_CLIENT_SECRET="..."
DEPARTMENTS='[{"name":"Engineering","workspace_id":"7c3f...","slug":"ws-engine-9749f1"}]'   # from step 4
TIER_KEY="metadata.jwt_tier"                                                               # from step 5
READ_ONLY="true"

OIDC_ISSUER_URL="https://example.okta.com"
OIDC_CLIENT_ID="0oa..."
OIDC_CLIENT_SECRET="..."
ADMIN_EMAILS="finops@example.com,ai-platform@example.com"
COOKIE_SECRET="<generated above>"
```
Every setting is described in [CONFIGURATION.md](CONFIGURATION.md). `config.env` is git-ignored; don't commit it.

## 9. Review the manifests
```bash
./deploy/deploy.sh --dry-run | less
```
This validates the config (missing values, slug-vs-UUID mistakes, bad JSON, cookie secret length) and prints all
manifests with secrets replaced by `***`. Nothing is applied. To validate against your cluster's API without applying:
```bash
python3 deploy/render.py deploy/config.env | kubectl apply --dry-run=server -f -
```

## 10. Deploy
```bash
./deploy/deploy.sh
```
Expected output ends with:
```
deployment "aigw-budget-admin" successfully rolled out
pod/aigw-budget-admin-...   2/2   Running
Open https://ai-budgets.example.com and sign in with one of: finops@example.com,...
```
What it creates in `NAMESPACE`: Secrets `aigw-budget-admin-scm` and `aigw-budget-admin-oidc`; ConfigMaps with the app
files, settings, admin list and sign-in templates; the Deployment (app + oauth2-proxy); the Service; the Ingress (if
`EXPOSE="ingress"`). Re-running is safe; it updates in place and restarts the pod when anything changed.

## 11. Verify
1. **Pod logs:**
   ```bash
   kubectl -n aigw-budget-admin logs deploy/aigw-budget-admin -c app
   # aigw-budget-admin listening on 127.0.0.1:8080 (demo=False, read_only=True, spend=logs, workspaces=N)
   kubectl -n aigw-budget-admin logs deploy/aigw-budget-admin -c oauth2-proxy | tail
   ```
2. **Sign in:** open `PUBLIC_URL` in a private window → your IdP → back to the dashboard. "Signed in as <email>" shows
   in the header. A user not in `ADMIN_EMAILS` gets the page "This account isn't an approved admin".
3. **Numbers:** compare a workspace's budget allocation and a user's budget usage with SCM (AI Gateway → workspace →
   Budget / budget policies). Spend is from request logs over `SPEND_WINDOW_HOURS`; policy counters are the gateway's
   own (see [HOW-IT-WORKS.md](HOW-IT-WORKS.md#numbers)).
4. **Refresh:** the page refreshes every 30 s; the **Refresh** button forces a reload from the API.

## 12. Enable budget resets (optional)
Resets write to your live tenant: they set one user's counter under one budget policy back to $0, which unblocks a
user who hit the limit.
```bash
sed -i.bak 's/^READ_ONLY=.*/READ_ONLY="false"/' deploy/config.env && ./deploy/deploy.sh
```
A **Reset** button appears next to each user with usage on a per-user budget. Click → confirm → the meter drops to $0,
the reset is listed under *Recent resets* (admin email, time, result) and logged:
```bash
kubectl -n aigw-budget-admin logs deploy/aigw-budget-admin -c app | grep AUDIT
```
If the reset returns an error, the service account's role lacks write access to budget policies (step 3). Set
`READ_ONLY="true"` again to turn resets off. The in-app audit list is in memory (cleared on restart); keep the pod logs
(your log platform) for a durable record.

## 13. Day 2: updates, secrets, uninstall
| Task | How |
|---|---|
| Change settings / add workspaces / admins | Edit `deploy/config.env`, run `./deploy/deploy.sh` |
| Upgrade | `git pull`, then `./deploy/deploy.sh` (app files are shipped as ConfigMaps; the pod restarts) |
| Rotate the SCM or OIDC client secret | Update `config.env`, run `./deploy/deploy.sh` |
| Sign everyone out | Change `COOKIE_SECRET`, run `./deploy/deploy.sh` |
| Pin / mirror images | `APP_IMAGE`, `PROXY_IMAGE` in `config.env` |
| Uninstall | `./deploy/deploy.sh --uninstall` (deletes the namespace), then delete the IdP app and the SCM service account |
