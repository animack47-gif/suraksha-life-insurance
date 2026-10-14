# Insurance-advisor leads -> scheduled visits (Hunar Voice Agents)

**Files:** `app.py` (whole pipeline) · `eval.py` (checks) · `.env` (your secrets, never commit)

## Setup (once)
```powershell
pip install -r requirements.txt
copy .env.example .env      # then edit .env:  HUNAR_API_KEY=...   LIVE_ALLOWLIST=+91<your own number>
```

## Run
```powershell
python app.py sample                 # makes sample_leads.xlsx (row 1 = your own number)
python app.py run sample_leads.xlsx  # ingest -> screening call -> scheduling call -> booking
python app.py poll                   # if it timed out, resume checking calls
python app.py report                 # funnel + results.xlsx (all leads + visit roster)
python eval.py                       # decision tests + call-quality + safety checks
```
Only numbers in `LIVE_ALLOWLIST` are dialled; everyone else shows `HELD_SAFETY`.
Progress is saved in `state.json`, so you can stop and resume. Delete it to start fresh.

## How it works (the 60-second explanation)
```
Excel -> clean/dedupe/filter -> [Agent 1: Screening call] -> rules in code -> [Agent 2: Scheduling call] -> book slot -> results.xlsx
```
1. **Ingest**: normalises phones to +91, drops duplicates, invalid numbers, under-18s, non-Mumbai leads.
2. **Agent 1 (screening)** asks: interested? 12th pass? 18+? can visit Mumbai? Returns structured JSON (`result_schema`).
3. **Rules (`decide_screening`)**: the AI *extracts facts*, plain Python *decides*. Unknown answers go to a human, never auto-qualify.
4. **Agent 2 (scheduling)** is given 3 real slots (`slot_options`), the candidate picks Option 1/2/3. Code maps that back to the slot *we* offered, so the agent can't invent dates.
5. **Booking** checks capacity (8 per session); if full or candidate declines, re-offer up to 3 times, then human follow-up.
6. **Results** are fetched by polling `GET /calls/{id}/` (no public webhook needed for a demo).

## Evals
- **Decision eval**: 16 golden cases (e.g. do-not-call beats interest; "option 4" of 3 must NOT book).
- **Call-quality eval**: re-fetches each call, checks the result has all fields with valid values, flags contradictions and calls under 20s; plus safety invariants (no slot over capacity, no scheduling after do-not-call).
- Hunar's docs I was given had no Eval endpoints, so these are local checks.

## Mocked / assumptions
Company, venue, slot days (Tue/Thu/Sat), times, capacity and the WhatsApp confirmation are placeholders. Calling hours/retries use your Hunar org defaults.

## Production next steps
Webhooks (`call_summary`) instead of polling · database instead of `state.json` · retries + calling-window guardrails · real WhatsApp/SMS · NDNC/consent checks · recruiter dashboard · multi-language per lead · bulk calls at scale.
