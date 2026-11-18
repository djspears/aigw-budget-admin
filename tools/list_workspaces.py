#!/usr/bin/env python3
"""Check the SCM service account and list AI Gateway workspaces with their budgets.

Prints each workspace (name, UUID, slug), its Budget Allocation, and its usage-limit (budget) policies with conditions
and grouping, then a ready-to-paste DEPARTMENTS line for config.env. Read-only.

Usage:
  python3 tools/list_workspaces.py deploy/config.env            # reads SCM_TSG_ID / SCM_CLIENT_ID / SCM_CLIENT_SECRET
  SCM_TSG_ID=... SCM_CLIENT_ID=... SCM_CLIENT_SECRET=... python3 tools/list_workspaces.py
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("AIGW_API_BASE", "https://api.apps.paloaltonetworks.com/ai_gw/v2").rstrip("/")
TOKEN_URL = os.environ.get("SCM_TOKEN_URL", "https://auth.apps.paloaltonetworks.com/oauth2/access_token")


def http(method, url, headers=None, form=None):
    data = urllib.parse.urlencode(form).encode() if form else None
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    if form:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]


def main():
    cfg = dict(os.environ)
    if len(sys.argv) > 1:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy"))
        import render
        cfg.update({k: v for k, v in render.load_env(sys.argv[1]).items() if v})
    missing = [k for k in ("SCM_TSG_ID", "SCM_CLIENT_ID", "SCM_CLIENT_SECRET") if not cfg.get(k)]
    if missing:
        sys.exit("missing " + ", ".join(missing))
    basic = base64.b64encode(f"{cfg['SCM_CLIENT_ID']}:{cfg['SCM_CLIENT_SECRET']}".encode()).decode()
    code, tok = http("POST", TOKEN_URL, {"Authorization": "Basic " + basic},
                     {"grant_type": "client_credentials", "scope": f"tsg_id:{cfg['SCM_TSG_ID']}"})
    if code != 200:
        sys.exit(f"SCM token request failed ({code}): {tok}\n-> check the client ID/secret and TSG ID")
    print("SCM token: OK")
    H = {"Authorization": f"Bearer {tok['access_token']}", "Accept": "application/json"}
    code, ws = http("GET", f"{API}/workspaces?page_size=100", H)
    if code != 200:
        sys.exit(f"GET /workspaces failed ({code}): {ws}\n-> the service account's role needs AI Gateway access")
    items = ws.get("data", ws) if isinstance(ws, dict) else ws
    deps = []
    for w in items:
        wid, slug = w.get("id"), w.get("slug")
        code, wd = http("GET", f"{API}/workspaces/{wid}", H)
        lims = [l for l in (wd.get("usage_limits") or [])] if code == 200 and isinstance(wd, dict) else []
        print(f"\n{w.get('name')}  id={wid}  slug={slug}")
        for l in lims:
            print(f"  budget allocation: ${l.get('credit_limit')} {l.get('type')} reset={l.get('periodic_reset') or l.get('periodic_reset_days') or 'none'}"
                  f" used=${l.get('current_usage')} status={l.get('status')}")
        if not lims:
            print("  budget allocation: none")
        code, pl = http("GET", f"{API}/policies/usage-limits?workspace_id={wid}", H)
        for p in (pl.get("data", []) if code == 200 and isinstance(pl, dict) else []):
            _, pd = http("GET", f"{API}/policies/usage-limits/{p['id']}?workspace_id={wid}", H)
            pd = pd if isinstance(pd, dict) else p
            print(f"  policy {pd.get('name')}: ${pd.get('credit_limit')} reset={pd.get('periodic_reset') or pd.get('periodic_reset_days') or 'none'}"
                  f" conditions={json.dumps(pd.get('conditions'))} group_by={[g.get('key') for g in pd.get('group_by') or []]}")
        if code != 200:
            print(f"  policies: HTTP {code}")
        deps.append({"name": w.get("name"), "workspace_id": wid, "slug": slug})
    print("\nDEPARTMENTS line for config.env (remove the workspaces you don't want):")
    print("DEPARTMENTS='" + json.dumps(deps, separators=(",", ":")) + "'")


if __name__ == "__main__":
    main()
