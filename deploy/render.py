#!/usr/bin/env python3
"""Render the Kubernetes manifests for AI Gateway Budget Admin from config.env (used by deploy.sh).

Usage: render.py <config.env> [--redact]
Prints one multi-document YAML to stdout. Secrets are only ever written to stdout (piped to kubectl), never to disk;
--redact replaces them with *** for review.
"""
import hashlib
import json
import os
import re
import shlex
import string
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SECRET_KEYS = ("SCM_CLIENT_SECRET", "OIDC_CLIENT_SECRET", "COOKIE_SECRET")


def load_env(path):
    """Parse a bash-style KEY="value" file (comments, single/double quotes) without executing it."""
    env = {}
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if not m:
            sys.exit(f"{path}:{n}: cannot parse: {line[:60]}")
        try:
            parts = shlex.split(m.group(2), comments=True)
        except ValueError as e:
            sys.exit(f"{path}:{n}: {e}")
        env[m.group(1)] = parts[0] if parts else ""
    return env


def fail(msg):
    sys.exit(f"config error: {msg}")


def validate(c):
    demo = c.get("DEMO") == "true"
    req = ["NAMESPACE", "PUBLIC_URL", "EXPOSE", "OIDC_ISSUER_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "ADMIN_EMAILS",
           "COOKIE_SECRET"] + ([] if demo else ["SCM_TSG_ID", "SCM_CLIENT_ID", "SCM_CLIENT_SECRET", "DEPARTMENTS"])
    missing = [k for k in req if not c.get(k)]
    if missing:
        fail("missing " + ", ".join(missing))
    if demo:
        c["DEPARTMENTS"] = ""
    for k in ("OIDC_CLIENT_SECRET",) + (() if demo else ("SCM_CLIENT_SECRET",)):
        if c[k] == "change-me":
            fail(f"{k} is still the example value")
    if len(c["COOKIE_SECRET"]) not in (16, 24, 32, 44):
        fail("COOKIE_SECRET must be 16, 24 or 32 bytes (or 32 bytes base64 = 44 chars); see config.env.example")
    u = urllib.parse.urlparse(c["PUBLIC_URL"])
    if u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/"):
        fail("PUBLIC_URL must look like https://host[:port] (no path)")
    if c["EXPOSE"] not in ("ingress", "loadbalancer", "clusterip"):
        fail("EXPOSE must be ingress, loadbalancer or clusterip")
    if demo:
        return None
    try:
        deps = json.loads(c["DEPARTMENTS"])
    except ValueError as e:
        fail(f"DEPARTMENTS is not valid JSON ({e}); keep it in single quotes")
    for d in deps:
        if not all(d.get(k) for k in ("name", "workspace_id", "slug")):
            fail(f"DEPARTMENTS entry needs name, workspace_id, slug: {d}")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", d["workspace_id"]):
            fail(f"workspace_id must be the workspace UUID, not the slug: {d['workspace_id']}")
    if c.get("READ_ONLY", "true") not in ("true", "false"):
        fail("READ_ONLY must be true or false")
    if u.scheme == "http" and c["EXPOSE"] == "ingress" and c.get("INGRESS_TLS_SECRET"):
        fail("PUBLIC_URL is http but INGRESS_TLS_SECRET is set; use https")
    return deps


def yaml_str(v):
    return json.dumps(v)                                                    # JSON strings are valid YAML scalars


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    redact = "--redact" in sys.argv
    c = {"KUBE_CONTEXT": "", "INGRESS_CLASS": "nginx", "INGRESS_TLS_SECRET": "", "LB_IP": "", "TIER_KEY": "metadata.jwt_tier",
         "SPEND_WINDOW_HOURS": "168", "READ_ONLY": "true", "TITLE_SUFFIX": "", "CACHE_SECONDS": "30",
         "APP_SUBTITLE": "Department and per-user LLM budgets", "OIDC_PROVIDER": "oidc", "OIDC_EMAIL_CLAIM": "email",
         "OIDC_SCOPE": "openid email profile", "OIDC_ALLOW_UNVERIFIED_EMAIL": "false", "IDP_LOGOUT_URL": "",
         "APP_IMAGE": "python:3.12-slim", "PROXY_IMAGE": "quay.io/oauth2-proxy/oauth2-proxy:v7.7.1"}
    c.update(load_env(sys.argv[1]))
    deps = validate(c)
    pub = urllib.parse.urlparse(c["PUBLIC_URL"])
    c["PUBLIC_URL"] = c["PUBLIC_URL"].rstrip("/")
    c["PUBLIC_HOST"] = pub.hostname
    c["COOKIE_SECURE"] = "true" if pub.scheme == "https" else "false"
    c["REVERSE_PROXY"] = "true" if c["EXPOSE"] == "ingress" else "false"
    if c["IDP_LOGOUT_URL"]:
        c["SIGNOUT_URL"] = "/oauth2/sign_out?rd=" + urllib.parse.quote(c["IDP_LOGOUT_URL"], safe="")
        lo = urllib.parse.urlparse(c["IDP_LOGOUT_URL"])
        c["LOGOUT_DOMAIN"] = lo.netloc
    else:
        c["SIGNOUT_URL"] = "/oauth2/sign_out"
        c["LOGOUT_DOMAIN"] = pub.netloc
    c["SERVICE_TYPE"] = {"loadbalancer": "LoadBalancer"}.get(c["EXPOSE"], "ClusterIP")
    c["SERVICE_ANNOTATIONS"] = (f"  annotations:\n    metallb.universe.tf/loadBalancerIPs: {c['LB_IP']}\n"
                                if c["EXPOSE"] == "loadbalancer" and c["LB_IP"] else "")
    c["INGRESS_TLS"] = (f"  tls:\n    - hosts: [{pub.hostname}]\n      secretName: {c['INGRESS_TLS_SECRET']}\n"
                        if c["INGRESS_TLS_SECRET"] else "")

    files = {k: open(os.path.join(ROOT, p), encoding="utf-8").read() for k, p in
             (("server.py", "app/server.py"), ("demo.py", "app/demo.py"), ("index.html", "app/static/index.html"),
              ("sign_in.html", "oauth2-proxy-templates/sign_in.html"), ("error.html", "oauth2-proxy-templates/error.html"))}
    settings = {"DEMO": c.get("DEMO", "false"), "SCM_TSG_ID": c.get("SCM_TSG_ID", ""),
                "DEPARTMENTS": json.dumps(deps) if deps else "", "SPEND_SOURCE": "logs",
                "SPEND_WINDOW_HOURS": c["SPEND_WINDOW_HOURS"], "TIER_KEY": c["TIER_KEY"], "READ_ONLY": c["READ_ONLY"],
                "TITLE_SUFFIX": c["TITLE_SUFFIX"], "APP_SUBTITLE": c["APP_SUBTITLE"], "SIGNOUT_URL": c["SIGNOUT_URL"],
                "CACHE_SECONDS": c["CACHE_SECONDS"]}
    admins = "\n".join(e.strip() for e in c["ADMIN_EMAILS"].split(",") if e.strip()) + "\n"
    h = hashlib.sha256(json.dumps([files, settings, admins, [c.get(k, "") for k in SECRET_KEYS + ("SCM_CLIENT_ID",)]]).encode()).hexdigest()[:16]
    c["CONFIG_HASH"] = h
    sec = (lambda v: "***") if redact else (lambda v: v)

    def cm(name, data):
        body = "\n".join(f"  {k}: {yaml_str(v)}" for k, v in data.items())
        return f"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {name}\n  namespace: {c['NAMESPACE']}\ndata:\n{body}\n"

    def secret(name, data):
        body = "\n".join(f"  {k}: {yaml_str(sec(v))}" for k, v in data.items())
        return f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: {name}\n  namespace: {c['NAMESPACE']}\ntype: Opaque\nstringData:\n{body}\n"

    docs = [f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {c['NAMESPACE']}\n  labels:\n"
            f"    pod-security.kubernetes.io/enforce: restricted\n",
            secret("aigw-budget-admin-scm", {"SCM_CLIENT_ID": c.get("SCM_CLIENT_ID", ""), "SCM_CLIENT_SECRET": c.get("SCM_CLIENT_SECRET", "")}),
            secret("aigw-budget-admin-oidc", {"OIDC_CLIENT_SECRET": c["OIDC_CLIENT_SECRET"], "COOKIE_SECRET": c["COOKIE_SECRET"]}),
            cm("aigw-budget-admin-settings", settings),
            cm("aigw-budget-admin-app", {k: files[k] for k in ("server.py", "demo.py", "index.html")}),
            cm("aigw-budget-admin-templates", {k: files[k] for k in ("sign_in.html", "error.html")}),
            cm("aigw-budget-admin-admins", {"admins.txt": admins})]
    tpl = ["10-deployment.yaml", "20-service.yaml"] + (["30-ingress.yaml"] if c["EXPOSE"] == "ingress" else [])
    for t in tpl:
        docs.append(string.Template(open(os.path.join(HERE, "templates", t), encoding="utf-8").read()).substitute(c))
    print("---\n".join(docs), end="")


if __name__ == "__main__":
    main()
