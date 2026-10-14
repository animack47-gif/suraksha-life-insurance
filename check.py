import json, app
st = app.load()
for cid, rec in st["calls"].items():
    c = app.api("GET", f"/calls/{cid}/")
    print(cid, rec["stage"])
    for k in ("status", "lifecycle_status", "from_phone_number", "next_retry_scheduled_at",
              "answered_by", "engagement_status", "duration_seconds", "started_at", "guardrails"):
        print("  ", k, "=", c.get(k))