#!/usr/bin/env bash
set -euo pipefail

BASE=${BASE:-http://localhost:8000}

login() {
  curl -sS -X POST "$BASE/auth/login" -H 'content-type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"$2\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

auth() { echo "Authorization: Bearer $1"; }

curl -sS -X POST "$BASE/demo/bootstrap" -H 'content-type: application/json' -d '{}' >/tmp/bootstrap.json
ENGINEER=$(login engineer@example.com engineer-password)
PUBLISHER=$(login publisher@example.com publisher-password)
VIEWER=$(login viewer@example.com viewer-password)
IMAGE_ID=$(python3 -c 'import json; print(json.load(open("/tmp/bootstrap.json"))["image_id"])')
MATRIX_ID=$(python3 -c 'import json; print(json.load(open("/tmp/bootstrap.json"))["matrix_version_id"])')

JOB=$(curl -sS -X POST "$BASE/jobs" -H "$(auth "$PUBLISHER")" -H 'content-type: application/json' \
  -d "{\"image_id\":\"$IMAGE_ID\",\"matrix_version_id\":\"$MATRIX_ID\",\"algorithm\":\"fista_nnls\"}")
JOB_ID=$(python3 -c "import json,sys; print(json.loads(sys.stdin.read())['id'])" <<<"$JOB")

for _ in $(seq 1 60); do
  STATUS=$(curl -sS "$BASE/jobs/$JOB_ID" -H "$(auth "$PUBLISHER")")
  STATE=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"$STATUS")
  [[ "$STATE" == "succeeded" || "$STATE" == "partial" || "$STATE" == "failed" ]] && break
  sleep 2
done

# Viewer authorization must fail even though source viewing is allowed.
set +e
curl -sS -f -X POST "$BASE/jobs/$JOB_ID/publish" -H "$(auth "$VIEWER")" -H 'content-type: application/json' -d '{}'
DENIED=$?
set -e
[[ "$DENIED" != "0" ]]

if [[ "$STATE" == "succeeded" ]]; then
  curl -sS -f -X POST "$BASE/jobs/$JOB_ID/publish" -H "$(auth "$PUBLISHER")" \
    -H 'content-type: application/json' -d '{}'
else
  echo "Job was $STATE; publish is expected to be rejected until all required tiles succeed."
  exit 2
fi

echo "ACCEPTANCE_OK job=$JOB_ID image=$IMAGE_ID matrix=$MATRIX_ID"
