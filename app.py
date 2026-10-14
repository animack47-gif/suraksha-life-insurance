"""Lead -> screening call -> scheduling call -> booked visit.   Usage:
    python app.py sample          # write sample_leads.xlsx (first row = YOUR allow-listed number)
    python app.py run sample_leads.xlsx  # ingest, call, wait for results, schedule, write results.xlsx
    python app.py poll            # resume: check pending calls again
    python app.py report          # funnel + results.xlsx
"""
import hashlib, json, os, re, sys, time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass



CALL_DAYS = [d.strip().upper() for d in os.getenv("CALL_DAYS", "MON,TUE,WED,THU,FRI,SAT").split(",")]
CALL_FROM = os.getenv("CALL_FROM", "20:00")          # HH:MM, window must be >= 3 hours
CALL_TO = os.getenv("CALL_TO", "19:00")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "0"))     # 0-10; 0 = no retries
RETRY_HOURS = int(os.getenv("RETRY_HOURS", "0"))     # one of 3, 6, 9, 12, 2



# ----------------------------------------------------------------------------- CONFIG (secrets only from .env)
KEY = os.getenv("HUNAR_API_KEY", "")
BASE = os.getenv("HUNAR_BASE_URL", "https://api.voice.hunar.ai/external/v1").rstrip("/")
ALLOW = {x.strip() for x in os.getenv("LIVE_ALLOWLIST", "").split(",") if x.strip()}   # safety: ONLY these get called
ALLOW_ALL = os.getenv("ALLOW_ALL_LEADS") == "1"
LANGUAGE = os.getenv("LANGUAGE", "HINDI").upper()            # HINDI | ENGLISH | MARATHI
COMPANY = os.getenv("COMPANY_NAME", "Suraksha Life Insurance")           # MOCK business details
VENUE = os.getenv("VENUE_NAME", "Suraksha Life Mumbai Recruitment Centre")
ADDRESS = os.getenv("VENUE_ADDRESS", "5th Floor, Tower B, BKC, Bandra East, Mumbai 400051")
VISIT_WEEKDAYS, SESSIONS, CAPACITY, MIN_LEAD_DAYS = (0, 2, 4), ("11:00", "15:00"), 8, 2   # Tue/Thu/Sat
STATE, TERMINAL = "state.json", {"COMPLETED", "NOT_CONNECTED", "FAILED", "CANCELLED"}
MMR = ("mumbai", "thane", "navi", "andheri", "bandra", "borivali", "kandivali", "malad", "goregaon", "dadar", "kurla",
       "ghatkopar", "powai", "vashi", "panvel", "kalyan", "dombivli", "mulund", "chembur", "vikhroli", "bhandup",
       "mira road", "vasai", "virar", "worli", "juhu", "airoli", "kharghar", "dahisar")

# ----------------------------------------------------------------------------- AGENTS (2 workflows)
SCREEN_PROMPT = """You are {persona_name}, a polite recruitment assistant for {company_name}, calling people who filled a form for our Insurance Advisor opportunity in {lead_city}. You are an AI assistant; say so if asked.
GOAL: in under 4 minutes find out if the person is interested and meets basic criteria. Do NOT book the visit; a colleague will call back.
RULES: one short question at a time, warm and never pushy; mirror the caller's language mix. Confirm you are speaking to {callee_name} and it is a good time (else ask for a callback time and end). Wrong person -> apologise and end. NEVER promise income or jobs (say details are explained in person). If asked to stop calling, apologise, confirm and end.
ASK IN ORDER: 1) interested in exploring the advisor role (yes/no/maybe)? 2) Is your highest education above 12th? 3) age 18 or above? 4) full-time or part-time? 5) can they travel to a Mumbai office for a short meeting? 6) any worries? Close politely."""
SCHED_PROMPT = """You are {persona_name} from {company_name}. {callee_name} was screened earlier and is eligible. You are calling ONLY to book an in-person visit. You are an AI assistant; say so if asked.
VENUE: {venue_name}, {venue_address}. SLOTS OFFERED: {slot_options}. The visit takes about 2 hours. Bring photo ID, Aadhaar, PAN, 12th marksheet, 2 passport photos.
RULES: be brief and friendly. Read the options (Option 1, 2, 3) and ask which suits. Repeat the chosen slot and get a clear 'yes' before treating it as confirmed. If none suit, ask for a preferred day/time and say a colleague will follow up; NEVER invent slots. Never promise income. If asked to stop calling, apologise and end."""
SCREEN_SCHEMA = {"interested": "yes | no | maybe", "education_12th_or_above": "yes | no | unknown",
                 "age_18_plus": "yes | no | unknown", "can_visit_mumbai_venue": "yes | no | unknown",
                 "callback_requested": "yes | no", "do_not_call": "yes | no", "summary": "one sentence"}
SCHED_SCHEMA = {"visit_confirmed": "yes | no | reschedule", "chosen_option": "1 | 2 | 3 | none",
                "preferred_alternative": "free text or empty", "do_not_call": "yes | no", "summary": "one sentence"}
INTRO = {
    "screening": {"ENGLISH": "Hello {callee_name}, this is {persona_name}, an AI assistant from {company_name}. You showed interest in our insurance advisor opportunity. Is this a good time for a quick chat?",
                  "HINDI": "नमस्ते {callee_name} जी, मैं {persona_name}, {company_name} की AI असिस्टेंट। आपने हमारे इंश्योरेंस एडवाइज़र के विज्ञापन पर फ़ॉर्म भरा था। क्या अभी दो-तीन मिनट बात हो सकती है?",
                  "MARATHI": "नमस्कार {callee_name}, मी {persona_name}, {company_name} ची AI असिस्टंट. तुम्ही आमची इन्शुरन्स ॲडव्हायझर जाहिरात पाहून फॉर्म भरला होता. आत्ता दोन-तीन मिनिटं बोलता येईल का?"},
    "scheduling": {"ENGLISH": "Hello {callee_name}, this is {persona_name} from {company_name}. I'm calling to book your visit to our Mumbai office. Do you have a minute?",
                   "HINDI": "नमस्ते {callee_name} जी, मैं {persona_name}, {company_name} से। मैं आपकी मुंबई ऑफ़िस विज़िट बुक करने के लिए कॉल कर रही हूँ। क्या एक मिनट बात हो सकती है?",
                   "MARATHI": "नमस्कार {callee_name}, मी {persona_name}, {company_name} कडून. तुमची मुंबई ऑफिस भेट बुक करण्यासाठी फोन केला आहे. एक मिनिट बोलता येईल का?"}}
AGENTS = {"screening": dict(title="Advisor Screening", prompt=SCREEN_PROMPT, schema=SCREEN_SCHEMA,
                            objective="Screen social-media leads for interest and basic eligibility to become insurance advisors."),
          "scheduling": dict(title="Visit Scheduling", prompt=SCHED_PROMPT, schema=SCHED_SCHEMA,
                             objective="Book an in-person visit at the Mumbai recruitment centre for a screened candidate.")}


def fresh(path):
    """Overwrite-safe: delete any existing file at `path` so the new write replaces it instead of erroring."""
    try:
        Path(path).unlink(missing_ok=True)
    except PermissionError:
        sys.exit(f"Cannot overwrite {path}: it is open in another program (e.g. Excel). Close it and re-run.")
    return path


def api(method, path, **kw):
    r = requests.request(method, BASE + path, headers={"X-API-Key": KEY}, timeout=30, **kw)
    if r.status_code != 200:
        raise RuntimeError(f"Hunar {r.status_code} on {method} {path}: {r.text[:300]}")
    return r.json()


def ensure_agents() -> dict:
    existing, page = {}, 1
    while True:
        res = api("GET", "/agents/", params={"page": page, "page_size": 100})
        print(res)
        for a in res["results"]:
            existing.setdefault(a["name"], a["id"])
        if not res.get("next"):
            break
        page += 1
    ids = {}
    for stage, sp in AGENTS.items():
        name = f"{sp['title']} - {LANGUAGE.title()}"
        body = {"name": name, "language": LANGUAGE, "voice_persona": "NEHA", "persona_name": "Priya",
                "agent_prompt": sp["prompt"], "objective": sp["objective"], "introduction": INTRO[stage][LANGUAGE],
                "result_prompt": "Extract only facts the candidate explicitly stated; use 'unknown' if not discussed. Use exactly the allowed values.",
                "result_schema": sp["schema"]}
        print(body)
        a = api("PUT", f"/agents/{existing[name]}/", json=body) if name in existing else api("POST", "/agents/", json=body)
        print(a)
        ids[stage] = a["id"]
    return ids

# ----------------------------------------------------------------------------- RULES (LLM extracts, code decides)
def yn(v):
    s = str(v).strip().lower() if v is not None else ""
    return "yes" if s in ("yes", "y", "true", "1") else "no" if s in ("no", "n", "false", "0") else None


def decide_screening(res):
    r = {str(k).lower(): v for k, v in (res or {}).items()}
    if not any(str(v).strip() for v in r.values()):
        return "NOT_REACHED", "empty_result"
    if yn(r.get("do_not_call")) == "yes":
        return "DO_NOT_CONTACT", "asked_to_stop"
    i = str(r.get("interested", "")).strip().lower()
    if i == "no":
        return "NOT_INTERESTED", "declined"
    #if yn(r.get("callback_requested")) == "yes" or i == "maybe":
    #    return "CALLBACK", "callback_requested"
    if yn(r.get("education_12th_or_above")) == "no":
        return "NOT_QUALIFIED", "below_12th"
    if yn(r.get("age_18_plus")) == "no":
        return "NOT_QUALIFIED", "under_18"
    if yn(r.get("can_visit_mumbai_venue")) == "no":
        return "NOT_QUALIFIED", "cannot_visit"
    if i == "yes" and yn(r.get("education_12th_or_above")) == "yes" and yn(r.get("can_visit_mumbai_venue")) == "yes":
        return "QUALIFIED", "meets_criteria"
    return "HUMAN_REVIEW", "incomplete_answers"          # unknown never auto-qualifies


def decide_scheduling(res, n_offered):
    r = {str(k).lower(): str(v).strip().lower() for k, v in (res or {}).items()}
    if not any(r.values()):
        return "NO_RESPONSE", None
    if r.get("do_not_call") == "yes":
        return "DO_NOT_CONTACT", None
    opt = r.get("chosen_option", "")
    if r.get("visit_confirmed") == "yes" and opt.isdigit() and 1 <= int(opt) <= n_offered:
        return "BOOK", int(opt)
    if r.get("visit_confirmed") == "no":
        return "DECLINED", None
    return "RE_OFFER", None                              # reschedule / ambiguous / option not offered

# ----------------------------------------------------------------------------- LEADS (Excel in)
ALIASES = {"name": ("name", "full name", "candidate"), "phone": ("phone", "mobile", "mobile number", "contact", "contact number"),
           "city": ("city", "location", "area"), "age": ("age",)}


def norm_phone(raw):
    d = re.sub(r"\D", "", re.sub(r"\.0$", "", str(raw or "").strip()))
    d = d[4:] if d.startswith("0091") else d[2:] if d.startswith("91") and len(d) == 12 else d[1:] if d.startswith("0") and len(d) == 11 else d
    return "+91" + d if re.fullmatch(r"[6-9]\d{9}", d) and len(set(d)) > 2 else None


def ingest(path, st):
    df = pd.read_excel(path, dtype=str)
    df.columns = [str(c).strip().lower() for c in df.columns]
    col = {k: next((c for c in df.columns if c in v), None) for k, v in ALIASES.items()}
    stats = {"rows": len(df), "new": 0, "duplicate": 0, "rejected": 0}
    for _, row in df.iterrows():
        g = lambda k: (str(row[col[k]]).strip() if col[k] and pd.notna(row[col[k]]) else "")
        phone, name, city, age = norm_phone(g("phone")), g("name"), g("city"), g("age")
        lid = "L" + hashlib.sha1((phone or f"bad{name}{g('phone')}").encode()).hexdigest()[:8]
        if lid in st["leads"]:
            stats["duplicate"] += 1
            continue
        why = ("invalid_phone" if not phone else "missing_name" if not name else
               "under_18" if age.isdigit() and int(age) < 18 else
               "outside_mumbai" if city and not any(k in city.lower() for k in MMR) else "")
        st["leads"][lid] = dict(name=name, phone=phone, city=city or "Mumbai", status="REJECTED" if why else "NEW",
                                reason=why, n_screening=0, n_scheduling=0, slot=None, screening=None)
        stats["rejected" if why else "new"] += 1
    return stats

# ----------------------------------------------------------------------------- SLOTS (capacity-safe)
def all_slots():
    out = []
    for i in range(MIN_LEAD_DAYS, MIN_LEAD_DAYS + 15):
        d = date.today() + timedelta(days=i)
        if d.weekday() in VISIT_WEEKDAYS:
            out += [f"{d.isoformat()}T{t}" for t in SESSIONS]
    return out


def label(slot_id):
    return datetime.fromisoformat(slot_id).strftime("%A, %d %B at %I:%M %p").replace(" 0", " ")


def booked(st, slot_id):
    return sum(1 for l in st["leads"].values() if l["slot"] == slot_id)


def offer(st, exclude=(), n=3):
    free = [s for s in all_slots() if booked(st, s) < CAPACITY and s not in exclude]
    picked, days = [], set()
    for s in free:                                       # spread across different days first
        if s[:10] not in days and len(picked) < n:
            picked.append(s); days.add(s[:10])
    picked += [s for s in free if s not in picked][: n - len(picked)]
    return sorted(picked)

# ----------------------------------------------------------------------------- CALLS


def place_call(st, ids, lid, stage, custom, offered=None):
    lead = st["leads"][lid]
    lead[f"n_{stage}"] += 1
    print("im inside def place_call")
    body = {
        "agent_id": ids[stage], "callee_name": lead["name"], "mobile_number": lead["phone"],
        "custom_data": {k: str(v) for k, v in custom.items()},       # API wants string values
        "request_id": f"{stage[:5]}-{lid}-{lead['n_' + stage]}",
        "timezone": "Asia/Kolkata",
        # "timezone": os.getenv("CALL_TIMEZONE", "Asia/Kolkata"),
        "guardrails": {"allowed_days": CALL_DAYS, "earliest_call_time": CALL_FROM, "last_call_time": CALL_TO}}
    if MAX_RETRIES:
        body["retry_config"] = {"max_retry_count": MAX_RETRIES, "retry_interval_hours": RETRY_HOURS}
    call = api("POST", "/calls/", json=body)
    st["calls"][call["id"]] = dict(lead=lid, stage=stage, offered=offered or [], done=False, empty=0)
    lead["status"] = f"CALLING_{stage.upper()}"
    print(f"call: {call}")
    print(f"  {stage:<10} call -> {lead['name']} ({lead['phone']})")


def start_scheduling(st, ids, lid, exclude=()):
    slots = offer(st, exclude)
    if not slots:
        st["leads"][lid]["status"] = "WAITLIST"
        return
    text = " ".join(f"Option {i}: {label(s)}." for i, s in enumerate(slots, 1))
    place_call(st, ids, lid, "scheduling", dict(company_name=COMPANY, venue_name=VENUE, venue_address=ADDRESS,
                                                slot_options=text), offered=slots)


def handle(st, ids, cid, call):
    rec, lead = st["calls"][cid], st["leads"][st["calls"][cid]["lead"]]
    lid, res = rec["lead"], call.get("result") or {}
    if call.get("status") == "COMPLETED" and not res and rec["empty"] < 5:
        rec["empty"] += 1                                  # result can lag the call end; wait a few polls
        return False
    rec["done"] = True
    unreached = call.get("lifecycle_status") != "COMPLETED" or call.get("answered_by") == "MACHINE" or not res
    if rec["stage"] == "screening":
        lead["screening"] = res
        lead["status"], lead["reason"] = ("NOT_REACHED", "no answer / no result") if unreached else decide_screening(res)
        if lead["status"] == "QUALIFIED":
            start_scheduling(st, ids, lid)
        return True
    if unreached:
        lead["status"], lead["reason"] = "QUALIFIED_UNBOOKED", "scheduling call not answered"
        return True
    action, opt = decide_scheduling(res, len(rec["offered"]))
    if action == "BOOK":
        slot = rec["offered"][opt - 1]
        if booked(st, slot) < CAPACITY:
            lead["slot"], lead["status"], lead["reason"] = slot, "VISIT_SCHEDULED", label(slot)
            print(f"  BOOKED {lead['name']}: {label(slot)}  | WhatsApp confirmation (mock) -> {lead['phone']}")
            return True
        action = "RE_OFFER"                                # slot filled meanwhile
    if action == "RE_OFFER" and lead["n_scheduling"] < 3:
        start_scheduling(st, ids, lid, exclude=tuple(rec["offered"]))
    elif action == "RE_OFFER":
        lead["status"], lead["reason"] = "HUMAN_FOLLOWUP", "no slot agreed after 3 attempts"
    else:
        lead["status"], lead["reason"] = {"DECLINED": "DROPPED", "DO_NOT_CONTACT": "DO_NOT_CONTACT"}.get(action, "QUALIFIED_UNBOOKED"), action
    return True


def poll(st, ids):
    n = 0
    for cid, rec in list(st["calls"].items()):
        if not rec["done"]:
            call = api("GET", f"/calls/{cid}/")
            if call.get("lifecycle_status") in TERMINAL:
                n += handle(st, ids, cid, call)
    return n


def pending(st):
    return sum(1 for r in st["calls"].values() if not r["done"])

# ----------------------------------------------------------------------------- STATE / REPORT / CLI
def load():
    return json.loads(Path(STATE).read_text()) if Path(STATE).exists() else {"leads": {}, "calls": {}}


def save(st):
    Path(STATE).write_text(json.dumps(st, indent=1, ensure_ascii=False))


def report(st):
    counts = pd.Series([l["status"] for l in st["leads"].values()]).value_counts()
    print("\nFUNNEL\n" + counts.to_string())
    rows = [dict(lead_id=k, **{x: v[x] for x in ("name", "phone", "city", "status", "reason", "slot")}) for k, v in st["leads"].items()]
    with pd.ExcelWriter(fresh("results.xlsx")) as w:
        pd.DataFrame(rows).to_excel(w, sheet_name="All leads", index=False)
        pd.DataFrame([r for r in rows if r["status"] == "VISIT_SCHEDULED"]).sort_values("slot").to_excel(w, sheet_name="Visit roster", index=False) \
            if any(r["status"] == "VISIT_SCHEDULED" for r in rows) else None
    print("wrote results.xlsx")


def wait_loop(st, ids, minutes=float(os.getenv("MAX_WAIT_MIN", "30"))):
    end = time.time() + minutes * 60
    while pending(st) and time.time() < end:
        n = poll(st, ids)
        save(st)
        if pending(st):
            print(f"  ...{pending(st)} call(s) still in progress")
            time.sleep(15)
    if pending(st):
        print("Timed out waiting; run `python app.py poll` later to continue.")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "sample":
        mine = next(iter(ALLOW), "+919999900001")
        pd.DataFrame([dict(Name="Test Candidate", Phone=mine, City="Andheri", Age=28)] + [
            dict(Name=n, Phone=p, City=c, Age=a) for n, p, c, a in [
                ("Rahul Sharma", "9820011111", "Thane", 31), ("Priya Patil", "98200 22222", "Dadar", 26),
                ("Duplicate Rahul", "+91 9820011111", "Thane", 31), ("Bad Number", "12345", "Mumbai", 30),
                ("Pune Lead", "9822033333", "Pune", 28), ("Minor", "9820044444", "Andheri", 17)]]
        ).to_excel(fresh("sample_leads.xlsx"), index=False)
        return print("wrote sample_leads.xlsx")
    if not KEY:
        sys.exit("Set HUNAR_API_KEY in .env first.")
    st = load()
    if cmd == "run":
        print("ingest:", ingest(argv[2], st))
        ids = ensure_agents()
        st["agents"] = ids
        for lid, lead in st["leads"].items():
            print(lid)
            print(lead)
            if lead["status"] != "NEW":
                continue
            if ALLOW_ALL or lead["phone"] in ALLOW:
                print("placing call ... ", lead["phone"])
                place_call(st, ids, lid, "screening", dict(company_name=COMPANY, lead_city=lead["city"]))
            else:
                lead["status"], lead["reason"] = "HELD_SAFETY", "not in LIVE_ALLOWLIST"
        save(st)
        wait_loop(st, ids)
    elif cmd == "poll":
        wait_loop(st, st["agents"])
    elif cmd != "report":
        return print(__doc__)
    save(st)
    report(st)


if __name__ == "__main__":
    main(sys.argv)