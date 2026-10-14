"""Evals.   python eval.py
  1) decision rules vs a golden set (offline)    2) call quality + safety invariants on the calls we made (needs API)."""
import sys
from collections import Counter

import app

GOLDEN_SCREENING = [  # (result from agent, expected decision)
    ({"interested": "yes", "education_12th_or_above": "yes", "age_18_plus": "yes", "can_visit_mumbai_venue": "yes"}, "QUALIFIED"),
    ({"interested": "no"}, "NOT_INTERESTED"),
    ({"interested": "yes", "education_12th_or_above": "yes", "can_visit_mumbai_venue": "yes", "do_not_call": "yes"}, "DO_NOT_CONTACT"),
    ({"interested": "maybe", "callback_requested": "yes"}, "CALLBACK"),
    ({"interested": "yes", "education_12th_or_above": "no", "can_visit_mumbai_venue": "yes"}, "NOT_QUALIFIED"),
    ({"interested": "yes", "education_12th_or_above": "yes", "can_visit_mumbai_venue": "no"}, "NOT_QUALIFIED"),
    ({"interested": "yes", "education_12th_or_above": "unknown", "can_visit_mumbai_venue": "yes"}, "HUMAN_REVIEW"),
    ({}, "NOT_REACHED"),
    ({"Interested": "Yes", "Education_12th_or_above": True, "Can_Visit_Mumbai_Venue": "Yes"}, "QUALIFIED"),
]
GOLDEN_SCHEDULING = [  # (result, n_offered, expected action)
    ({"visit_confirmed": "yes", "chosen_option": "2"}, 3, "BOOK"),
    ({"visit_confirmed": "yes", "chosen_option": "4"}, 3, "RE_OFFER"),     # option never offered -> must NOT book
    ({"visit_confirmed": "yes", "chosen_option": "none"}, 3, "RE_OFFER"),
    ({"visit_confirmed": "no", "chosen_option": "none"}, 3, "DECLINED"),
    ({"visit_confirmed": "reschedule"}, 3, "RE_OFFER"),
    ({"visit_confirmed": "yes", "chosen_option": "1", "do_not_call": "yes"}, 3, "DO_NOT_CONTACT"),
    ({}, 3, "NO_RESPONSE"),
]
ENUMS = {"screening": {"interested": {"yes", "no", "maybe"}, "education_12th_or_above": {"yes", "no", "unknown"},
                       "can_visit_mumbai_venue": {"yes", "no", "unknown"}, "do_not_call": {"yes", "no"}},
         "scheduling": {"visit_confirmed": {"yes", "no", "reschedule"}, "chosen_option": {"1", "2", "3", "none"},
                        "do_not_call": {"yes", "no"}}}


def eval_decisions() -> int:
    fails = 0
    for res, exp in GOLDEN_SCREENING:
        got = app.decide_screening(res)[0]
        fails += got != exp
        print("PASS" if got == exp else "FAIL", "screening", res, "->", got)
    for res, n, exp in GOLDEN_SCHEDULING:
        got = app.decide_scheduling(res, n)[0]
        fails += got != exp
        print("PASS" if got == exp else "FAIL", "scheduling", res, "->", got)
    total = len(GOLDEN_SCREENING) + len(GOLDEN_SCHEDULING)
    print(f"decision eval: {total - fails}/{total} passed\n")
    return fails


def eval_calls() -> int:
    st = app.load()
    if not st["calls"] or not app.KEY:
        print("call eval skipped (no calls yet / no API key)")
        return 0
    stats, issues = Counter(), Counter()
    for cid, rec in st["calls"].items():
        call = app.api("GET", f"/calls/{cid}/")
        res = {str(k).lower(): str(v).strip().lower() for k, v in (call.get("result") or {}).items()}
        s = rec["stage"]
        stats[f"{s}_calls"] += 1
        if call.get("lifecycle_status") != "COMPLETED" or not res:
            stats[f"{s}_unreached"] += 1
            continue
        for k, allowed in ENUMS[s].items():
            if not res.get(k):
                issues[f"{s}:missing_{k}"] += 1
            elif res[k] not in allowed:
                issues[f"{s}:bad_value_{k}"] += 1
        if s == "scheduling" and res.get("chosen_option", "none").isdigit() and res.get("visit_confirmed") != "yes":
            issues["scheduling:option_without_confirmation"] += 1
        if (call.get("duration_seconds") or 0) < 20:
            issues[f"{s}:call_under_20s"] += 1
    print("call stats:", dict(stats), "\nresult issues:", dict(issues) or "none")
    bad = []                                              # hard safety invariants from our own state
    for sid in {l["slot"] for l in st["leads"].values() if l["slot"]}:
        if app.booked(st, sid) > app.CAPACITY:
            bad.append(f"over_capacity:{sid}")
    for lid, l in st["leads"].items():
        if l["status"] == "DO_NOT_CONTACT" and l["n_scheduling"] > 0:
            bad.append(f"scheduled_after_dnc:{lid}")
        if l["status"] == "VISIT_SCHEDULED" and not l["slot"]:
            bad.append(f"scheduled_without_slot:{lid}")
    print("invariant violations:", bad or "none")
    return len(bad)


if __name__ == "__main__":
    sys.exit(1 if (eval_decisions() + eval_calls()) else 0)
