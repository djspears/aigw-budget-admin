"""Demo mode (DEMO=true): synthetic AI Gateway API responses so the UI can be previewed without a tenant.

Only fakes the HTTP layer (workspaces, usage-limit policies, entities, reset, request logs); everything above it is the
real code path. Data is deterministic and regenerated relative to "now" at startup.
"""
import random
import re
import time
import uuid

TIERS = {"t1": 50, "t2": 150, "t3": 400}                                   # weekly per-user limits ($)
MODELS = [("claude-sonnet-4-5-20250929", 0.45), ("claude-haiku-4-5-20251001", 0.25), ("gpt-4o-2024-08-06", 0.2),
          ("claude-opus-4-1-20250805", 0.1)]
PRICE = {"claude-sonnet-4-5-20250929": 2.4, "claude-haiku-4-5-20251001": 0.6, "gpt-4o-2024-08-06": 1.8,
         "claude-opus-4-1-20250805": 9.0}                                   # avg cents... per 1k tokens-ish, scaled below

DEPARTMENTS = [
    {"name": "Engineering", "workspace_id": "00000000-0000-4000-8000-0000000000e1", "slug": "ws-engine-demo01",
     "budget": 5000, "users": [("alice.nguyen", "t3", 0.9), ("bob.martinez", "t2", 1.15), ("chen.li", "t2", 0.4),
                               ("dana.okafor", "t1", 1.3), ("evan.schmidt", "t1", 0.2)]},
    {"name": "Finance", "workspace_id": "00000000-0000-4000-8000-0000000000f1", "slug": "ws-financ-demo02",
     "budget": 1500, "users": [("farah.haddad", "t2", 0.55), ("george.kim", "t1", 0.7), ("hana.sato", "t1", 0.1)]},
    {"name": "Customer Support", "workspace_id": "00000000-0000-4000-8000-0000000000c1", "slug": "ws-custom-demo03",
     "budget": 2000, "users": [("ivan.petrov", "t1", 0.85), ("julia.rossi", "t2", 0.3), ("kofi.mensah", "t1", 0.0)]},
]


def public_departments():
    return [{"name": d["name"], "workspace_id": d["workspace_id"], "slug": d["slug"]} for d in DEPARTMENTS]


class Demo:
    def __init__(self, tier_key):
        self.tier_meta = tier_key.replace("metadata.", "")
        self.tier_key = tier_key
        rnd = random.Random(42)
        now = time.time()
        week_start = now - now % 86400 - time.gmtime(now).tm_wday * 86400
        self.ws, self.policies, self.entities, self.logs = {}, {}, {}, {}
        for d in DEPARTMENTS:
            wid = d["workspace_id"]
            recs, per_user = [], {}
            for user, tier, intensity in d["users"]:
                target = TIERS[tier] * intensity * 100                     # cents of spend this week
                spent = 0.0
                while spent < target:
                    model = rnd.choices([m for m, _ in MODELS], [w for _, w in MODELS])[0]
                    cost = round(PRICE[model] * rnd.uniform(4, 40), 4)
                    ts = rnd.uniform(max(week_start, now - 6.5 * 86400), now - 60)
                    recs.append(self._rec(d, user, tier, model, cost, ts, 200))
                    spent += cost
                    if rnd.random() < 0.03:                                # a few blocked requests (cost 0)
                        recs.append(self._rec(d, user, tier, model, 0, ts + 5, 446))
                per_user[user] = (tier, spent / 100)
            recs.sort(key=lambda r: r["created_at"], reverse=True)
            self.logs[d["slug"]] = recs
            total = sum(s for _, s in per_user.values())
            self.ws[wid] = {"id": wid, "name": d["name"], "slug": d["slug"], "last_reset_at": None,
                            "usage_limits": [{"id": str(uuid.UUID(int=rnd.getrandbits(128))), "type": "cost",
                                              "status": "active", "credit_limit": d["budget"],
                                              "current_usage": round(total, 4), "periodic_reset": "weekly",
                                              "alert_threshold": round(d["budget"] * 0.8)}]}
            self.policies[wid] = []
            for tier, limit in TIERS.items():
                pid = str(uuid.UUID(int=rnd.getrandbits(128)))
                self.policies[wid].append({
                    "id": pid, "name": f"{d['name'].lower().replace(' ', '_')}_budget_{tier}", "type": "cost", "credit_limit": limit,
                    "alert_threshold": round(limit * 0.8), "periodic_reset": "weekly", "periodic_reset_days": None,
                    "next_usage_reset_at": time.strftime("%Y-%m-%dT00:00:00.000Z", time.gmtime(week_start + 7 * 86400)),
                    "conditions": [{"key": tier_key, "value": [tier]}], "group_by": [{"key": "metadata._user"}],
                    "status": "active", "workspace_id": wid, "object": "usage-limit"})
                self.entities[pid] = [{"id": str(uuid.UUID(int=rnd.getrandbits(128))), "value_key": f"metadata._user:{u}",
                                       "current_usage": round(min(s, limit), 4), "status": "active"}
                                      for u, (t, s) in per_user.items() if t == tier and s > 0]

    def _rec(self, d, user, tier, model, cost, ts, status):
        keys = ["_user", self.tier_meta, "workspace_slug", "auth_type"]
        vals = [user, tier, d["slug"], "JWT"]
        return {"_user": user, "ai_model": model, "cost": cost, "cost_unit": "cents", "response_status_code": status,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)), "workspace_slug": d["slug"],
                "metadataKey": keys, "metadataValue": vals}

    def gw(self, method, url, body=None):
        path = url.split("/ai_gw/v2", 1)[-1]
        q = dict(p.split("=", 1) for p in path.split("?", 1)[1].split("&")) if "?" in path else {}
        path = path.split("?", 1)[0]
        m = re.fullmatch(r"/workspaces/([^/]+)", path)
        if m and method == "GET":
            w = self.ws.get(m.group(1))
            return (200, w) if w else (404, "not found")
        if path == "/policies/usage-limits" and method == "GET":
            # like the real API, the list omits conditions/group_by
            return 200, {"data": [{k: v for k, v in p.items() if k not in ("conditions", "group_by")}
                                  for p in self.policies.get(q.get("workspace_id"), [])]}
        m = re.fullmatch(r"/policies/usage-limits/([^/]+)", path)
        if m and method == "GET":
            p = next((p for ps in self.policies.values() for p in ps if p["id"] == m.group(1)), None)
            return (200, p) if p else (404, "not found")
        m = re.fullmatch(r"/policies/usage-limits/([^/]+)/entities", path)
        if m and method == "GET":
            return 200, {"data": self.entities.get(m.group(1), [])}
        m = re.fullmatch(r"/policies/usage-limits/([^/]+)/entities/([^/]+)/reset", path)
        if m and method == "PUT":
            for e in self.entities.get(m.group(1), []):
                if e["id"] == m.group(2):
                    e["current_usage"] = 0.0
                    return 200, {}
            return 404, "not found"
        if path == "/logs" and method == "GET":
            recs = self.logs.get(q.get("workspaceSlug"), [])
            page, size = int(q.get("currentPage", 0)), int(q.get("pageSize", 500))
            return 200, {"data": {"records": recs[page * size:(page + 1) * size], "total": len(recs)}}
        return 404, f"demo: no route for {method} {path}"
