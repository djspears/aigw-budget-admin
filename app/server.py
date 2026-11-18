#!/usr/bin/env python3
"""AI Gateway Budget Admin: backend (Python standard library only).

Serves the single-page UI and a small JSON API:
  GET  /api/overview          departments, budget policies, per-user usage, per-user x model usage, roster
  POST /api/reset             {"workspace_id", "policy_id", "entity_id", "value_key"} -> resets one budget counter
  GET  /api/audit             recent resets (in memory; also logged to stdout)
  GET  /healthz

Data sources:
  - Prisma AIRS AI Gateway management API (AIGW_API_BASE), SCM service-account bearer token:
      workspaces (budget allocation), usage-limit policies + entities, request logs
  - Optional Keycloak admin REST (KC_URL set; read-only service account): users, groups (= departments), user_tier attribute.
  - Spend: Jaeger traces of the gateway (SPEND_SOURCE=jaeger) or the AI Gateway request logs API (SPEND_SOURCE=logs).

Auth: runs behind oauth2-proxy (any OIDC IdP, email allowlist). The proxy sets X-Forwarded-Email /
X-Forwarded-User, which are recorded in the audit log. The app listens on localhost only.
"""
import base64
import html
import json
import calendar
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = os.environ.get("AIGW_API_BASE", "https://api.apps.paloaltonetworks.com/ai_gw/v2").rstrip("/")
POL, WS, LOGS = f"{API}/policies/usage-limits", f"{API}/workspaces", f"{API}/logs"
TOKEN_URL = os.environ.get("SCM_TOKEN_URL", "https://auth.apps.paloaltonetworks.com/oauth2/access_token")
DEMO = os.environ.get("DEMO", "false").lower() == "true"                 # true: synthetic data, no tenant needed
SPEND_SOURCE = os.environ.get("SPEND_SOURCE", "logs")                    # logs (AI Gateway request logs API) | jaeger
TIER_KEY = os.environ.get("TIER_KEY", "metadata.jwt_tier")                # metadata key the tier budget policies match on
READ_ONLY = os.environ.get("READ_ONLY", "true").lower() == "true"        # true: budget resets disabled
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LISTEN = os.environ.get("LISTEN", "127.0.0.1:8080")
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "30"))


def _load_departments():
    """DEPARTMENTS: JSON list of {"name", "workspace_id" (UUID), "slug", "group" (optional Keycloak group)}."""
    raw = os.environ.get("DEPARTMENTS")
    if not raw and DEMO:
        import demo
        return demo.public_departments()
    if not raw:
        raise SystemExit("config error: DEPARTMENTS is not set (JSON list of workspaces)")
    try:
        deps = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"config error: DEPARTMENTS is not valid JSON: {e}")
    for d in deps:
        missing = [k for k in ("name", "workspace_id", "slug") if not d.get(k)]
        if missing:
            raise SystemExit(f"config error: DEPARTMENTS entry {d} is missing {missing}")
        d.setdefault("group", "")
    return deps


DEPARTMENTS = _load_departments()
if not DEMO:
    _missing = [k for k in ("SCM_CLIENT_ID", "SCM_CLIENT_SECRET", "SCM_TSG_ID") if not os.environ.get(k)]
    if _missing:
        raise SystemExit(f"config error: missing {', '.join(_missing)}")
_demo = None
if DEMO:
    import demo
    _demo = demo.Demo(TIER_KEY)

_lock = threading.Lock()
_token = {"value": None, "exp": 0}
_cache = {"data": None, "at": 0}
_audit = []


# ------------------------------------------------------------------ HTTP helpers

def _http(method, url, headers=None, body=None, form=None, timeout=30):
    h = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]


def scm_token():
    with _lock:
        if _token["value"] and time.time() < _token["exp"] - 60:
            return _token["value"]
        cid, sec = os.environ["SCM_CLIENT_ID"], os.environ["SCM_CLIENT_SECRET"]
        code, t = _http("POST", TOKEN_URL, form={"grant_type": "client_credentials",
                                                 "scope": f"tsg_id:{os.environ['SCM_TSG_ID']}"},
                        headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()})
        if code != 200:
            raise RuntimeError(f"SCM token failed: {code} {t}")
        _token.update(value=t["access_token"], exp=time.time() + int(t.get("expires_in", 900)))
        return _token["value"]


def gw(method, url, body=None):
    if _demo:
        return _demo.gw(method, url, body)
    return _http(method, url, headers={"Authorization": f"Bearer {scm_token()}", "Accept": "application/json"}, body=body)


def keycloak_roster():
    """{username: {email, name, enabled, tier, department}} from the Keycloak realm (read-only)."""
    if not os.environ.get("KC_URL"):
        return {}                                                          # no IdP roster: users come from gateway logs
    base, realm = os.environ["KC_URL"].rstrip("/"), os.environ["KC_REALM"]
    code, t = _http("POST", f"{base}/realms/{realm}/protocol/openid-connect/token",
                    form={"grant_type": "client_credentials", "client_id": os.environ["KC_CLIENT_ID"],
                          "client_secret": os.environ["KC_CLIENT_SECRET"]})
    if code != 200:
        return {}
    auth = {"Authorization": f"Bearer {t['access_token']}"}
    roster = {}
    for d in DEPARTMENTS:
        code, groups = _http("GET", f"{base}/admin/realms/{realm}/groups?search={urllib.parse.quote(d['group'])}", headers=auth)
        g = next((x for x in groups or [] if x.get("name") == d["group"]), None) if code == 200 else None
        if not g:
            continue
        code, members = _http("GET", f"{base}/admin/realms/{realm}/groups/{g['id']}/members?max=500&briefRepresentation=false", headers=auth)
        for m in members or []:
            attrs = m.get("attributes") or {}
            local = (m.get("email") or m["username"]).split("@")[0]   # gateway _user = email local part
            roster[local] = {
                "username": m["username"], "email": m.get("email"), "enabled": m.get("enabled", True),
                "name": " ".join(x for x in (m.get("firstName"), m.get("lastName")) if x) or m["username"],
                "tier": (attrs.get("user_tier") or [None])[0], "department": d["name"],
            }
    return roster


# ------------------------------------------------------------------ actual spend from gateway OTel traces (Jaeger)

JAEGER_URL = os.environ.get("JAEGER_URL", "")
JAEGER_SERVICE = os.environ.get("JAEGER_SERVICE", "jwt-aigw")
SPEND_WINDOW_HOURS = int(os.environ.get("SPEND_WINDOW_HOURS", "168"))
PROVIDERS = {"gcp.vertex_ai": "Vertex AI", "aws.bedrock": "Bedrock", "azure.ai.inference": "Azure AI", "azure.ai.openai": "Azure OpenAI",
             "openai": "OpenAI", "anthropic": "Anthropic"}


def friendly_model(raw):
    """'us.anthropic.claude-sonnet-4-5-20250929-v1:0' / 'claude-sonnet-4-5@20250929' -> 'Claude Sonnet 4.5'."""
    import re
    m = (raw or "unknown").lower().split("/")[-1]
    m = re.sub(r"^(us|eu|apac|global)\.", "", m).replace("anthropic.", "")
    c = re.search(r"claude-(opus|sonnet|haiku)-(\d+)(?:-(\d))?(?=[-@.:]|$)", m) or re.search(r"claude-(\d+)(?:-(\d))?-(opus|sonnet|haiku)", m)
    if c:
        g = c.groups()
        family, major, minor = (g[0], g[1], g[2]) if g[0] in ("opus", "sonnet", "haiku") else (g[2], g[0], g[1])
        return f"Claude {family.title()} {major}{'.' + minor if minor else ''}"
    g = re.search(r"gpt-([\w.\-]+?)(?:-\d{4}-\d{2}-\d{2})?$", m)
    if g:
        return "GPT-" + g.group(1)                                         # gpt-4o -> GPT-4o, gpt-4.1-mini -> GPT-4.1-mini
    return raw


def trace_spend():
    """{workspace_slug: {"total", "models": {model: $}, "providers": {model: {provider: $}}, "users": {user: {model: $}}}}"""
    if not JAEGER_URL:
        return {}
    now = int(time.time() * 1_000_000)
    q = urllib.parse.urlencode({"service": JAEGER_SERVICE, "limit": "2000", "lookback": f"{SPEND_WINDOW_HOURS}h",
                                "start": str(now - SPEND_WINDOW_HOURS * 3600 * 1_000_000), "end": str(now)})
    code, body = _http("GET", f"{JAEGER_URL.rstrip('/')}/api/traces?{q}", timeout=60)
    if code != 200 or not isinstance(body, dict):
        return {}
    out = {}
    for tr in body.get("data") or []:
        for sp in tr.get("spans", []):
            tags = {t["key"]: t["value"] for t in sp.get("tags", [])}
            if "gen_ai.operation.name" not in tags or "portkey.usage.cost" not in tags:
                continue                                                   # one gen_ai span per provider attempt
            cost = float(tags.get("portkey.usage.cost") or 0) / 100       # Portkey cost is in cents
            if cost <= 0:
                continue                                                   # failed attempts / blocked requests
            ws = tags.get("portkey.workspace.slug") or tags.get("portkey.metadata.workspace_slug")
            user = tags.get("portkey.metadata._user") or "unknown"
            model = friendly_model(tags.get("gen_ai.response.model") or tags.get("gen_ai.request.model"))
            prov = PROVIDERS.get(tags.get("gen_ai.provider.name"), tags.get("gen_ai.provider.name") or "")
            w = out.setdefault(ws, {"total": 0.0, "models": {}, "providers": {}, "users": {}, "costs": []})
            w["total"] += cost
            w["costs"].append((sp.get("startTime", 0), cost))
            w["models"][model] = w["models"].get(model, 0) + cost
            w["providers"].setdefault(model, {})[prov] = w["providers"].get(model, {}).get(prov, 0) + cost
            u = w["users"].setdefault(user, {})
            u[model] = u.get(model, 0) + cost
    return out


def log_spend():
    """Same shape as trace_spend(), from the AI Gateway request logs (cost in cents), plus per-user tier from metadata."""
    end = time.time(); start = end - SPEND_WINDOW_HOURS * 3600
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
    tier_meta = TIER_KEY.replace("metadata.", "")
    out = {}
    for d in DEPARTMENTS:
        w = {"total": 0.0, "models": {}, "providers": {}, "users": {}, "costs": [], "tiers": {}, "requests": 0}
        page = 0
        while True:
            q = urllib.parse.urlencode({"organisationId": os.environ.get("SCM_TSG_ID", "0"), "workspaceSlug": d["slug"],
                                        "timeOfGenerationMin": iso(start), "timeOfGenerationMax": iso(end),
                                        "pageSize": "500", "currentPage": str(page)})
            if _demo:
                code, body = _demo.gw("GET", f"{LOGS}?{q}")
            else:
                code, body = _http("GET", f"{LOGS}?{q}", headers={"Authorization": f"Bearer {scm_token()}",
                                                                  "x-tsg-id": os.environ["SCM_TSG_ID"]}, timeout=60)
            recs = (body or {}).get("data", {}).get("records", []) if code == 200 and isinstance(body, dict) else []
            for r in recs:
                md = dict(zip(r.get("metadataKey") or [], r.get("metadataValue") or []))
                user = r.get("_user") or md.get("_user") or "unknown"
                w["requests"] += 1
                if md.get(tier_meta):
                    w["tiers"][user] = md[tier_meta]
                cost = float(r.get("cost") or 0) / (100 if (r.get("cost_unit") or "cents") == "cents" else 1)
                w["users"].setdefault(user, {})
                if cost <= 0:
                    continue                                               # blocked / failed requests
                model = friendly_model(r.get("ai_model"))
                ts = calendar.timegm(time.strptime(r["created_at"][:19], "%Y-%m-%dT%H:%M:%S")) * 1_000_000
                w["total"] += cost
                w["costs"].append((ts, cost))
                w["models"][model] = w["models"].get(model, 0) + cost
                w["users"][user][model] = w["users"][user].get(model, 0) + cost
            if len(recs) < 500 or page >= 20:
                break
            page += 1
        out[d["slug"]] = w
    return out


# ------------------------------------------------------------------ data assembly

def _entities(dept, policy):
    code, e = gw("GET", f"{POL}/{policy['id']}/entities?workspace_id={dept['workspace_id']}&page_size=100")
    return e.get("data", []) if code == 200 and isinstance(e, dict) else []


def _policy_detail(dept, policy):
    code, p = gw("GET", f"{POL}/{policy['id']}?workspace_id={dept['workspace_id']}")
    return p if code == 200 and isinstance(p, dict) else policy


def _parse_key(value_key):
    """'metadata._user:frank|model:claude…' or 'metadata._user:frank' -> dict of dimension -> value."""
    out = {}
    for part in value_key.split("|"):
        k, _, v = part.partition(":")
        out[k.replace("metadata.", "")] = v
    return out


def _period_start(limit, ws):
    """Start of the workspace budget's current period (epoch s): daily resets roll at 00:00 UTC."""
    now = time.time()
    days = limit.get("periodic_reset_days")
    last = ws.get("last_reset_at")
    last_s = calendar.timegm(time.strptime(last[:19], "%Y-%m-%dT%H:%M:%S")) if last else 0
    if days:
        start = now - now % 86400 if days == 1 else now - days * 86400
        return max(start, last_s)
    if limit.get("periodic_reset") == "weekly":                           # weekly resets roll Monday 00:00 UTC
        day0 = now - now % 86400
        return max(day0 - time.gmtime(now).tm_wday * 86400, last_s)
    if limit.get("periodic_reset") == "monthly":
        g = time.gmtime(now)
        return max(calendar.timegm((g.tm_year, g.tm_mon, 1, 0, 0, 0)), last_s)
    return last_s


def workspace_budget(d, trace):
    """Workspace Budget Allocation (SCM) from GET /ai_gw/v2/workspaces/{id}: limit, used, period."""
    code, ws = gw("GET", f"{WS}/{d['workspace_id']}")
    if code != 200 or not isinstance(ws, dict):
        return None
    lim = next((l for l in ws.get("usage_limits") or [] if l.get("type") == "cost" and l.get("status") == "active"), None)
    if not lim:
        return {"limit": None}
    start = _period_start(lim, ws)
    if lim.get("current_usage") is not None:                              # gateway's own counter, when reported
        used, source = float(lim["current_usage"]), "gateway"
    else:                                                                  # otherwise sum of trace spend this period
        used = sum(c for t, c in (trace or {}).get("costs", []) if t / 1_000_000 >= start)
        source = "traces"
    days = lim.get("periodic_reset_days")
    return {"limit": float(lim["credit_limit"]), "used": round(used, 4), "source": source,
            "alert": lim.get("alert_threshold") or None,
            "period": "daily" if days == 1 else (f"every {days} days" if days else (lim.get("periodic_reset") or "no reset")),
            "period_start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)) if start else None}


def build_overview():
    roster = keycloak_roster()
    departments = []

    def load_dept(d):
        code, lst = gw("GET", f"{POL}?workspace_id={d['workspace_id']}")
        policies = lst.get("data", []) if code == 200 and isinstance(lst, dict) else []
        out = {"name": d["name"], "slug": d["slug"], "workspace_id": d["workspace_id"], "error": None if code == 200 else f"{code}",
               "budget_policies": [], "department_budget": None, "users": {}, "models": {}}
        with ThreadPoolExecutor(8) as pool:
            fetched = list(pool.map(lambda pol: (_policy_detail(d, pol), _entities(d, pol)), policies))
        for p, ents in fetched:
            groups = [g["key"] for g in p.get("group_by", [])]
            entry = {"id": p["id"], "name": p["name"], "type": p["type"], "limit": float(p["credit_limit"]),
                     "alert": p.get("alert_threshold"), "reset": p.get("periodic_reset") or (f"{p['periodic_reset_days']}d" if p.get("periodic_reset_days") else "none"),
                     "next_reset": p.get("next_usage_reset_at"), "conditions": p.get("conditions", []), "group_by": groups}
            model_cond = next((c for c in entry["conditions"] if c["key"] == "model"), None)
            if groups in (["metadata.workspace_slug"], ["workspace_id"], ["metadata.jwt_department"]):   # department budget (one bucket)
                used = sum(float(e.get("current_usage") or 0) for e in ents)
                ent = ents[0] if ents else None
                out["department_budget"] = {**entry, "used": used, "entity_id": ent and ent["id"], "value_key": ent and ent["value_key"]}
            elif model_cond and groups == ["metadata._user"]:               # metering: one policy per model (pattern)
                vals = model_cond["value"] if isinstance(model_cond["value"], list) else [model_cond["value"]]
                model = ", ".join(v.strip("@").rstrip("/*") for v in vals)
                for e in ents:
                    user = _parse_key(e["value_key"]).get("_user")
                    used = float(e.get("current_usage") or 0)
                    u = out["users"].setdefault(user, {"budgets": [], "models": {}})
                    u["models"][model] = u["models"].get(model, 0) + used
                    out["models"][model] = out["models"].get(model, 0) + used
                out.setdefault("models", {}).setdefault(model, out["models"].get(model, 0))
            elif "model" in groups and "metadata._user" in groups:          # metering: user x model (if the CP populates it)
                for e in ents:
                    k = _parse_key(e["value_key"]); user, model = k.get("_user"), k.get("model")
                    u = out["users"].setdefault(user, {"budgets": [], "models": {}})
                    u["models"][model] = u["models"].get(model, 0) + float(e.get("current_usage") or 0)
                    out["models"][model] = out["models"].get(model, 0) + float(e.get("current_usage") or 0)
            elif groups == ["metadata._user"]:                              # per-user budget (e.g. tiers)
                out["budget_policies"].append(entry)
                for e in ents:
                    user = _parse_key(e["value_key"]).get("_user")
                    u = out["users"].setdefault(user, {"budgets": [], "models": {}})
                    used = float(e.get("current_usage") or 0)
                    u["budgets"].append({"policy_id": p["id"], "policy": p["name"], "limit": entry["limit"], "used": used,
                                         "entity_id": e["id"], "value_key": e["value_key"], "exhausted": used >= entry["limit"]})
        # actual spend by model from gateway traces (overrides policy-based metering when available)
        t = tf.result().get(d["slug"])                                   # waits only here for the Jaeger query
        out["workspace_budget"] = workspace_budget(d, t)
        out["trace_window_hours"] = SPEND_WINDOW_HOURS if (JAEGER_URL or SPEND_SOURCE == "logs") else None
        out["spend_source"] = "gateway logs" if SPEND_SOURCE == "logs" else "gateway traces"
        if t:
            out["models"], out["providers"], out["trace_spend"] = t["models"], t["providers"], t["total"]
            t.pop("costs", None)
            for user, models in t["users"].items():
                out["users"].setdefault(user, {"budgets": [], "models": {}})["models"] = models
        elif JAEGER_URL or SPEND_SOURCE == "logs":
            out["models"], out["providers"], out["trace_spend"] = {}, {}, 0.0
        # roster users with no usage yet
        for local, info in roster.items():
            if info["department"] == d["name"]:
                out["users"].setdefault(local, {"budgets": [], "models": {}})
        for local, u in out["users"].items():
            info = roster.get(local, {})
            u.update(user=local, name=info.get("name", local), email=info.get("email"),
                     tier=info.get("tier") or (t or {}).get("tiers", {}).get(local),
                     enabled=info.get("enabled", True))
            # which tier policy should apply (condition match), even before first spend
            for bp in out["budget_policies"]:
                for c in bp["conditions"]:
                    vals = c["value"] if isinstance(c["value"], list) else [c["value"]]
                    if c["key"] == TIER_KEY and u["tier"] in vals and not any(b["policy_id"] == bp["id"] for b in u["budgets"]):
                        u["budgets"].append({"policy_id": bp["id"], "policy": bp["name"], "limit": bp["limit"], "used": 0.0,
                                             "entity_id": None, "value_key": None, "exhausted": False})
            # primary budget first: the policy whose tier condition matches the user's tier, else the closest to
            # its limit; drop $0 leftovers from policies that don't apply (e.g. after a condition was corrected)
            conds = {bp["id"]: bp["conditions"] for bp in out["budget_policies"]}

            def applies(b):
                cs = conds.get(b["policy_id"], [])
                tier_cs = [c for c in cs if c["key"] == TIER_KEY]
                if not tier_cs:
                    return True
                return any(u["tier"] in (c["value"] if isinstance(c["value"], list) else [c["value"]]) for c in tier_cs)

            u["budgets"] = [b for b in u["budgets"] if applies(b) or b["used"] > 0]
            u["budgets"].sort(key=lambda b: (not applies(b), -(b["used"] / b["limit"] if b["limit"] else 0)))
            u["spend"] = max([b["used"] for b in u["budgets"]] + [sum(u["models"].values())] + [0])
        out["users"] = sorted(out["users"].values(), key=lambda x: (-x["spend"], x["user"]))
        if "trace_spend" in out:
            out["spend"] = out["trace_spend"]
            for u in out["users"]:
                u["spend"] = sum(u["models"].values())
        else:
            out["spend"] = (out["department_budget"] or {}).get("used") or sum(out["models"].values()) or sum(u["spend"] for u in out["users"])
        out["users"] = sorted(out["users"], key=lambda x: (-x["spend"], x["user"]))
        return out


    with ThreadPoolExecutor(1) as tex:                                     # Jaeger query runs alongside the policy calls
        tf = tex.submit(log_spend if SPEND_SOURCE == "logs" else trace_spend)
        with ThreadPoolExecutor(len(DEPARTMENTS)) as ex:
            departments = list(ex.map(load_dept, DEPARTMENTS))
    users = [u for d in departments for u in d["users"]]
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "totals": {"spend": round(sum(d["spend"] for d in departments), 4), "departments": len(departments),
                   "users": len(users), "exhausted": sum(1 for u in users if any(b["exhausted"] for b in u["budgets"]))},
        "departments": departments,
        "read_only": READ_ONLY,
        "demo": DEMO,
        "user_source": "idp" if os.environ.get("KC_URL") else "logs",
    }


_build_lock = threading.Lock()


def _rebuild():
    with _build_lock:                                                      # one rebuild at a time
        _cache.update(data=build_overview(), at=time.time())


def overview(force=False):
    """Serve the cached snapshot; rebuild synchronously only when forced or when there is none yet."""
    if force or not _cache["data"]:
        _rebuild()
    elif time.time() - _cache["at"] > CACHE_SECONDS and not _build_lock.locked():
        threading.Thread(target=_rebuild, daemon=True).start()            # refresh in the background
    return _cache["data"]


def _refresher():
    while True:
        time.sleep(CACHE_SECONDS)
        try:
            _rebuild()
        except Exception as e:                                             # keep serving the last good snapshot
            print("refresh failed:", e, flush=True)


def reset(req, actor):
    if READ_ONLY:
        return 403, {"error": "read-only deployment: budget resets are disabled"}
    dept = next((d for d in DEPARTMENTS if d["workspace_id"] == req.get("workspace_id")), None)
    if not dept or not req.get("policy_id") or not req.get("entity_id"):
        return 400, {"error": "workspace_id, policy_id and entity_id are required"}
    code, r = gw("PUT", f"{POL}/{req['policy_id']}/entities/{req['entity_id']}/reset?workspace_id={dept['workspace_id']}", {})
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "actor": actor, "department": dept["name"],
           "policy_id": req["policy_id"], "value_key": req.get("value_key"), "result": code}
    _audit.insert(0, rec)
    del _audit[200:]
    print("AUDIT", json.dumps(rec), flush=True)
    _cache["at"] = 0                                                       # refresh on next read
    return (200, {"ok": True}) if code == 200 else (502, {"error": f"gateway returned {code}", "detail": r})


# ------------------------------------------------------------------ HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = "aigw-budget-admin"

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)

    def _actor(self):
        return self.headers.get("X-Forwarded-Email") or self.headers.get("X-Forwarded-User") or "unknown"

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, {"ok": True})
        if path == "/api/overview":
            try:
                force = "refresh=1" in (urllib.parse.urlparse(self.path).query or "")
                return self._send(200, {**overview(force), "actor": self._actor()})
            except Exception as e:                                       # surface upstream failures to the UI
                return self._send(502, {"error": str(e)})
        if path == "/api/audit":
            return self._send(200, {"items": _audit})
        file = "index.html" if path in ("/", "/index.html") else None
        if file:
            with open(os.path.join(STATIC, file), encoding="utf-8") as f:
                page = f.read()
            for k, default in (("TITLE_SUFFIX", ""), ("APP_SUBTITLE", "Department and per-user LLM budgets"),
                               ("SIGNOUT_URL", "/oauth2/sign_out")):
                page = page.replace(f"__{k}__", html.escape(os.environ.get(k, default), quote=True))
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/reset":
            return self._send(404, {"error": "not found"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(415, {"error": "Content-Type must be application/json"})   # blocks cross-site form posts
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid JSON"})
        code, body = reset(req, self._actor())
        self._send(code, body)

    def log_message(self, fmt, *args):
        if "/healthz" not in (args[0] if args else ""):
            print("%s %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    host, port = LISTEN.rsplit(":", 1)
    print(f"aigw-budget-admin listening on {LISTEN} (demo={DEMO}, read_only={READ_ONLY}, spend={SPEND_SOURCE}, "
          f"workspaces={len(DEPARTMENTS)})", flush=True)
    threading.Thread(target=_refresher, daemon=True).start()
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
