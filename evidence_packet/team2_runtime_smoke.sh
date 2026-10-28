#!/usr/bin/env bash
# Team 2 runtime smoke — Pravesh bridge (both apps) + Sampada identity +
# Sampada -> HackaVerse live enrolment + CV builder approval-gated export.
set -u
# Overridable so the same script runs against any local/CI stack. Defaults
# keep the original evidence ports; local dev uses 8000 (Sampada) and 8100
# (HackaVerse):  HR_URL=http://127.0.0.1:8000 HV_URL=http://127.0.0.1:8100 ./team2_runtime_smoke.sh
HR=${HR_URL:-http://127.0.0.1:8123}
HV=${HV_URL:-http://127.0.0.1:8017}
GWKEY=${GWKEY:-team2_smoke_gw_key}
# Pravesh shared secret used to mint probe cookies. Defaults to the gateway's
# configured PRAVESH_JWT_SECRET (backend/.env) so the script never embeds one.
PRAVESH_SMOKE_SECRET=${PRAVESH_SMOKE_SECRET:-$(grep '^PRAVESH_JWT_SECRET=' "$(dirname "$0")/../backend/.env" 2>/dev/null | cut -d= -f2- | tr -d '\r')}
SECRET=${PRAVESH_SMOKE_SECRET:-team2_smoke_secret}
# HackaVerse backend API key (read, never printed) — run this script from the repo root
HVKENV=${HACKAVERSE_ENV:-../bhiv-Hackaverse-main/hackathon/.env}
HVKKEY=$(grep '^API_KEY=' "$HVKENV" | cut -d= -f2- | tr -d '\r')

echo "================ 1. PRAVESH BRIDGE — SAMPADA GATEWAY ================"
echo "-- status:"; curl -s $HR/api/v1/pravesh/status; echo
TOK_S=$(python - "$SECRET" <<'EOF'
import sys, json, base64, hmac, hashlib, time
secret = sys.argv[1].encode()
def b(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')
h=b({'alg':'HS256','typ':'JWT'})
p=b({'user_id':'bhiv_smoke_1','email':'smoke@example.com','tenant_id':'tenant_default','roles':['candidate'],'permissions':['apps:read'],'allowedApps':['sampada','hackaverse'],'exp':int(time.time())+600})
s=base64.urlsafe_b64encode(hmac.new(secret,f'{h}.{p}'.encode(),hashlib.sha256).digest()).decode().rstrip('=')
print(f'{h}.{p}.{s}')
EOF
)
echo "-- session with valid cookie (expect 200):"; curl -s -o /tmp/o1 -w "%{http_code}\n" $HR/api/v1/pravesh/session -H "Cookie: blackhole_token=$TOK_S"; cat /tmp/o1; echo
echo "-- session without cookie (expect 401):"; curl -s -o /dev/null -w "%{http_code}\n" $HR/api/v1/pravesh/session
TOK_BAD=$(python - "$SECRET" <<'EOF'
import sys, json, base64, hmac, hashlib, time
secret = sys.argv[1].encode()
def b(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')
h=b({'alg':'HS256','typ':'JWT'})
p=b({'user_id':'bhiv_smoke_1','email':'smoke@example.com','allowedApps':['hackaverse'],'exp':int(time.time())+600})
s=base64.urlsafe_b64encode(hmac.new(secret,f'{h}.{p}'.encode(),hashlib.sha256).digest()).decode().rstrip('=')
print(f'{h}.{p}.{s}')
EOF
)
echo "-- session with token whose allowedApps omits sampada:"
CODE2=$(curl -s -o /tmp/o2 -w "%{http_code}" $HR/api/v1/pravesh/session -H "Cookie: blackhole_token=$TOK_BAD")
if [ "${STRICT_APP_CLAIM:-0}" = "1" ]; then
  echo "   STRICT_APP_CLAIM=1 -> expect 403, got $CODE2"; cat /tmp/o2; echo
else
  echo "   default policy -> expect 200 (one Blackhole sign-in opens every product), got $CODE2"
fi

echo "================ 2. PRAVESH BRIDGE — HACKAVERSE ================"
echo "-- status:"; curl -s $HV/pravesh/status; echo
echo "-- session with valid cookie (expect 200):"; curl -s -o /tmp/o3 -w "%{http_code}\n" $HV/pravesh/session -H "Cookie: blackhole_token=$TOK_S"; cat /tmp/o3; echo
echo "-- session without cookie (expect 401):"; curl -s -o /dev/null -w "%{http_code}\n" $HV/pravesh/session

echo "================ 3. CV BUILDER — FULL FLOW ================"
AUTH="Authorization: Bearer $GWKEY"
echo "-- templates:"; curl -s $HR/api/v1/cv/templates -H "$AUTH" | head -c 400; echo
echo "-- prefill (no authorised record -> honest blanks + questions):"
curl -s "$HR/api/v1/cv/prefill" -H "$AUTH" | python -c "import sys,json; d=json.load(sys.stdin); print('missing:',d['missing_fields'][:4]); print('first question:',d['questions'][0]['question'][:80]); print('summary field:',d['prefill']['professional_summary'])"
DRAFT=$(curl -s -X POST $HR/api/v1/cv/drafts -H "$AUTH" -H 'Content-Type: application/json' -d '{"template":"professional"}')
DID=$(echo "$DRAFT" | python -c "import sys,json; print(json.load(sys.stdin)['draft']['draft_id'])")
echo "-- created draft: $DID"
CONTENT=$(python - <<'EOF'
import json
sections = [
 {"id":"header","type":"header","heading":"Header","visible":True,"entries":[{"id":"e1","text":"Asha Patil","subtext":"asha@example.com · Pune"}]},
 {"id":"summary","type":"summary","heading":"Professional Summary","visible":True,"entries":[{"id":"e2","text":"  backend   engineer. honest words only. "}]},
 {"id":"experience","type":"experience","heading":"Work Experience","visible":True,"entries":[]},
]
print(json.dumps({"schema_version":"cv-1.0","sections":sections}))
EOF
)
curl -s -X PUT $HR/api/v1/cv/drafts/$DID -H "$AUTH" -H 'Content-Type: application/json' -d "{\"template\":\"modern\",\"content\":$CONTENT}" -o /tmp/o4 -w "PUT content+template -> %{http_code}\n"
echo "-- template switch keeps content:"
curl -s $HR/api/v1/cv/drafts/$DID -H "$AUTH" | python -c "import sys,json; d=json.load(sys.stdin)['draft']; print('template:',d['template']); print('summary text:',repr(d['content']['sections'][1]['entries'][0]['text']))"
echo "-- suggest (bounded, review-before-apply):"
curl -s -X POST $HR/api/v1/cv/suggest -H "$AUTH" -H 'Content-Type: application/json' -d '{"text":"  backend   engineer. honest words only. ","field_ref":"summary:e2"}' | python -c "import sys,json; d=json.load(sys.stdin); print('proposals:',len(d['proposals'])); print(d['proposals'][0]['proposed'] if d['proposals'] else '(none)'); print('policy:',d['policy'])"
echo "-- assemble preview:"; curl -s -X POST $HR/api/v1/cv/assemble -H "$AUTH" -H 'Content-Type: application/json' -d "{\"content\":$CONTENT,\"template\":\"professional\"}" | python -c "import sys,json; d=json.load(sys.stdin); print('html starts:',d['preview_html'][:40]); print('warnings:',[w['code'] for w in d['validation']['warnings']])"
echo "-- export BEFORE approval (expect 403):"; curl -s -o /tmp/o5 -w "%{http_code}\n" -X POST $HR/api/v1/cv/export -H "$AUTH" -H 'Content-Type: application/json' -d "{\"draft_id\":\"$DID\"}"; cat /tmp/o5; echo
echo "-- approve with wrong phrase (expect 422):"; curl -s -o /dev/null -w "%{http_code}\n" -X POST $HR/api/v1/cv/drafts/$DID/approve -H "$AUTH" -H 'Content-Type: application/json' -d '{"confirmation":"looks good"}'
echo "-- approve with exact phrase (expect 200):"; curl -s -o /dev/null -w "%{http_code}\n" -X POST $HR/api/v1/cv/drafts/$DID/approve -H "$AUTH" -H 'Content-Type: application/json' -d '{"confirmation":"I have reviewed this CV"}'
echo "-- export AFTER approval (expect 200):"; curl -s -X POST $HR/api/v1/cv/export -H "$AUTH" -H 'Content-Type: application/json' -d "{\"draft_id\":\"$DID\"}" | python -c "import sys,json; d=json.load(sys.stdin); print('print_ready:',d['print_ready'],'filename:',d['filename']); print('contains name:','Asha Patil' in d['html'])"
echo "-- recruiter/other-user access to this draft (expect 404 for foreign key):"
curl -s -o /dev/null -w "GET foreign draft -> %{http_code}\n" $HR/api/v1/cv/drafts/cv_foreign_key -H "$AUTH"

echo "================ 4. SAMPADA IDENTITY + LIVE ENROLMENT TO HACKAVERSE ================"
INTAKE=$(curl -s -X POST $HR/api/v1/identity/abhiruchi/intake -H "$AUTH" -H 'Content-Type: application/json' -d '{"source_system":"abhiruchi","source_reference":"ABH-SMOKE-1","email":"student@example.com","institute_code":"RTMSSU","cohort_id":"MSSU-2026-B1","trace_id":"trace-team2-smoke-1"}')
echo "-- intake #1:"; echo "$INTAKE" | python -c "import sys,json; d=json.load(sys.stdin); print('status:',d['status'],'id:',d['record']['sampada_local_id'],'canonical:',d['record']['canonical_id_status'],'institute:',d['record']['institute_code'],'warnings:',d['warnings'])"
SID=$(echo "$INTAKE" | python -c "import sys,json; print(json.load(sys.stdin)['record']['sampada_local_id'])")
echo "-- intake #2 (same source_reference = retry, expect reused, no duplicate):"
curl -s -X POST $HR/api/v1/identity/abhiruchi/intake -H "$AUTH" -H 'Content-Type: application/json' -d '{"source_system":"abhiruchi","source_reference":"ABH-SMOKE-1","email":"student@example.com","institute_code":"MSSU","cohort_id":"MSSU-2026-B1"}' | python -c "import sys,json; d=json.load(sys.stdin); print('status:',d['status'],'id:',d['record']['sampada_local_id'])"
echo "-- publish enrolment to HackaVerse (real HTTP):"
PUB=$(curl -s -X POST $HR/api/v1/identity/enrolment/publish -H "$AUTH" -H 'Content-Type: application/json' -d "{\"sampada_local_id\":\"$SID\",\"trace_id\":\"trace-team2-smoke-1\"}")
echo "$PUB" | python -c "import sys,json; d=json.load(sys.stdin); print('delivery:',d['delivery'],'hackaverse_status:',d['hackaverse_status'],'detail:',d['detail']); print('idempotency_key:',d['envelope']['idempotency_key'])"
KEY=$(echo "$PUB" | python -c "import sys,json; print(json.load(sys.stdin)['envelope']['idempotency_key'])")
echo "-- publish #2 (same key, expect HackaVerse reuse, no duplicate):"
curl -s -X POST $HR/api/v1/identity/enrolment/publish -H "$AUTH" -H 'Content-Type: application/json' -d "{\"sampada_local_id\":\"$SID\",\"trace_id\":\"trace-team2-smoke-1\"}" | python -c "import sys,json; d=json.load(sys.stdin); print('delivery:',d['delivery'],'hackaverse_status:',d['hackaverse_status'])"
echo "-- HackaVerse stored enrolment:"
curl -s "$HV/enrolment/key/$KEY" -H "X-API-Key: $HVKKEY" | python -c "import sys,json; d=json.load(sys.stdin); e=d['enrolment']; print('storage:',d['storage_mode'],'ingest_count:',e['ingest_count']); c=e['envelope']['candidate']; print('sampada_local_id:',c['sampada_local_id'],'institute:',c['institute_code'],'cohort:',c['cohort_id'],'canonical_status:',c['canonical_id_status'])"
echo "-- HackaVerse cohort listing:"
curl -s "$HV/enrolment/cohort/MSSU-2026-B1" -H "X-API-Key: $HVKKEY" | python -c "import sys,json; d=json.load(sys.stdin); print('cohort count:',d['count'])"
echo "-- invalid institute on HackaVerse side (expect 422):"
curl -s -o /dev/null -w "%{http_code}\n" -X POST $HV/enrolment/ingest -H "X-API-Key: $HVKKEY" -H 'Content-Type: application/json' -d '{"schema_version":"1.0","event":"candidate.enrolment.v1","source_system":"sampada","idempotency_key":"bad:1","candidate":{"sampada_local_id":"sd_x","institute_code":"NOT-A-CODE"}}'
echo "-- wrong schema_version (expect 422):"
curl -s -o /dev/null -w "%{http_code}\n" -X POST $HV/enrolment/ingest -H "X-API-Key: $HVKKEY" -H 'Content-Type: application/json' -d '{"schema_version":"9.9","event":"candidate.enrolment.v1","source_system":"sampada","idempotency_key":"bad:2","candidate":{"sampada_local_id":"sd_x"}}'

echo "================ SMOKE COMPLETE ================"
