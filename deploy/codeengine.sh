#!/usr/bin/env bash
# Deploy, or update, the reservation board on IBM Cloud Code Engine (free tier).
#
# Run from the repo folder on your Mac, after logging in to IBM Cloud:
#   ibmcloud login --sso
#   ./deploy/codeengine.sh
#
# Safe to run again any time: after editing config.cloud.json, or after a git pull to deploy a new version.
# It creates (only if missing):
#   - an IBM Cloud Object Storage instance (Standard plan) and a Smart Tier bucket (free tier: 5 GB for 12 months)
#   - a Writer service key the app uses to save reservations in the bucket
#   - a Code Engine project, a secret with your settings, and the app (scales to zero when idle)
#
# Optional overrides:  REGION=us-east RESOURCE_GROUP=Default ./deploy/codeengine.sh
set -euo pipefail
cd "$(dirname "$0")/.."

STATE_FILE=".cloud-deploy.env"
# shellcheck disable=SC1090
[ -f "$STATE_FILE" ] && . "./$STATE_FILE"

REGION="${REGION:-us-south}"
RESOURCE_GROUP="${RESOURCE_GROUP:-Default}"
PROJECT="${CE_PROJECT:-demo-reservations}"
APP="${APP_NAME:-reservations}"
COS_INSTANCE="${COS_INSTANCE:-demo-reservations-cos}"
COS_KEY_NAME="${COS_KEY_NAME:-demo-reservations-writer}"
SECRET="${SECRET_NAME:-reservations-settings}"
CONFIG_FILE="${CONFIG_FILE:-config.cloud.json}"
BUCKET="${BUCKET:-}"
ADMIN_KEY="${ADMIN_KEY:-}"

say()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); $1"; }
save_state() {
  umask 077
  cat > "$STATE_FILE" <<EOF
# Created by deploy/codeengine.sh. Keep this file private (it's in .gitignore).
REGION=$REGION
RESOURCE_GROUP=$RESOURCE_GROUP
CE_PROJECT=$PROJECT
APP_NAME=$APP
COS_INSTANCE=$COS_INSTANCE
BUCKET=$BUCKET
ADMIN_KEY=$ADMIN_KEY
EOF
}

# ------------------------------------------------------------------ prerequisites
command -v ibmcloud >/dev/null 2>&1 || die "The IBM Cloud CLI isn't installed. See https://cloud.ibm.com/docs/cli"
command -v python3  >/dev/null 2>&1 || die "python3 is needed to read IBM Cloud's JSON output"
[ -f server.py ] && [ -f Dockerfile ] || die "Run this from the repo folder (where server.py and Dockerfile are)"

say "Checking your IBM Cloud login"
ibmcloud iam oauth-tokens >/dev/null 2>&1 || die "You're not logged in. Run:  ibmcloud login --sso   then run this script again."
ACCOUNT="$(ibmcloud target --output json 2>/dev/null | json 'print(d.get("account", {}).get("name", "?"))' || echo '?')"
info "Account: $ACCOUNT"

say "Making sure the Code Engine and Object Storage CLI plugins are installed"
for plugin in code-engine cloud-object-storage; do
  ibmcloud plugin show "$plugin" >/dev/null 2>&1 || ibmcloud plugin install "$plugin" -f -q
done

say "Targeting region $REGION, resource group $RESOURCE_GROUP"
ibmcloud target -r "$REGION" -g "$RESOURCE_GROUP" -q >/dev/null \
  || die "Couldn't target that region and resource group. List yours with: ibmcloud resource groups"

# ------------------------------------------------------------------ settings file
if [ ! -f "$CONFIG_FILE" ]; then
  cp config.example.json "$CONFIG_FILE"
  info "Created $CONFIG_FILE from config.example.json (sign-in is off for the first deploy)."
fi
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$CONFIG_FILE" 2>/dev/null \
  || die "$CONFIG_FILE isn't valid JSON. Check for a missing comma or quote."

[ -n "$BUCKET" ]    || BUCKET="demo-reservations-$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
[ -n "$ADMIN_KEY" ] || ADMIN_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
save_state

# ------------------------------------------------------------------ Object Storage
say "Object Storage instance: $COS_INSTANCE"
if ibmcloud resource service-instance "$COS_INSTANCE" -q >/dev/null 2>&1; then
  info "Already exists"
else
  ibmcloud resource service-instance-create "$COS_INSTANCE" cloud-object-storage standard global -g "$RESOURCE_GROUP" -q \
    || die "Couldn't create the Object Storage instance. Free accounts need a credit card on file (Pay-as-you-go)."
fi
COS_CRN="$(ibmcloud resource service-instance "$COS_INSTANCE" --output json | json 'print(d[0]["crn"])')"

say "Bucket: $BUCKET (Smart Tier, region $REGION)"
ibmcloud cos config crn --crn "$COS_CRN" --force >/dev/null
if ibmcloud cos bucket-head --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
  info "Already exists"
else
  ibmcloud cos bucket-create --bucket "$BUCKET" --ibm-service-instance-id "$COS_CRN" --class smart --region "$REGION" >/dev/null \
    || ibmcloud cos bucket-create --bucket "$BUCKET" --ibm-service-instance-id "$COS_CRN" --class Smart --region "$REGION" >/dev/null \
    || die "Couldn't create bucket $BUCKET"
  info "Created"
fi
COS_ENDPOINT="https://s3.${REGION}.cloud-object-storage.appdomain.cloud"

say "Service key the app uses to read and write the bucket: $COS_KEY_NAME"
if ibmcloud resource service-key "$COS_KEY_NAME" -q >/dev/null 2>&1; then
  info "Already exists"
else
  ibmcloud resource service-key-create "$COS_KEY_NAME" Writer --instance-name "$COS_INSTANCE" -q >/dev/null \
    || die "Couldn't create the service key"
  info "Created (role: Writer)"
fi
COS_APIKEY="$(ibmcloud resource service-key "$COS_KEY_NAME" --output json | json 'print(d[0]["credentials"]["apikey"])')"

# ------------------------------------------------------------------ Code Engine
say "Code Engine project: $PROJECT"
if ibmcloud ce project get --name "$PROJECT" >/dev/null 2>&1; then
  ibmcloud ce project select --name "$PROJECT" >/dev/null
  info "Selected"
else
  ibmcloud ce project create --name "$PROJECT" >/dev/null || die "Couldn't create the Code Engine project"
  info "Created"
fi

say "Storing your settings as the Code Engine secret: $SECRET"
secret_args=(
  --from-file "RESERVE_CONFIG_JSON=$CONFIG_FILE"
  --from-literal "COS_BUCKET=$BUCKET"
  --from-literal "COS_ENDPOINT=$COS_ENDPOINT"
  --from-literal "COS_APIKEY=$COS_APIKEY"
  --from-literal "RESERVE_ADMIN_KEY=$ADMIN_KEY"
)
if ibmcloud ce secret get --name "$SECRET" >/dev/null 2>&1; then
  ibmcloud ce secret update --name "$SECRET" "${secret_args[@]}" >/dev/null
  info "Updated"
else
  ibmcloud ce secret create --name "$SECRET" --format generic "${secret_args[@]}" >/dev/null
  info "Created"
fi

# Small and scale-to-zero keeps it inside the free tier. max-scale 1 keeps a single copy of the data.
app_args=(
  --build-source .
  --build-strategy dockerfile
  --port 8080
  --min-scale 0
  --max-scale 1
  --cpu 0.125
  --memory 0.25G
  --scale-down-delay 120
)
say "Building and deploying the app: $APP (this takes 3 to 6 minutes)"
if ibmcloud ce app get --name "$APP" >/dev/null 2>&1; then
  ibmcloud ce app update --name "$APP" "${app_args[@]}"
else
  ibmcloud ce app create --name "$APP" "${app_args[@]}" --env-from-secret "$SECRET" --visibility public
fi

URL="$(ibmcloud ce app get --name "$APP" --output url)"
say "Checking the app"
HEALTH="$(curl -s --max-time 60 "$URL/healthz" || true)"
info "${HEALTH:-no answer yet (it may still be starting; try the URL in a minute)}"

AUTH_ON="$(python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1])).get("auth", {}).get("enabled", False)).lower())' "$CONFIG_FILE")"
printf '\n======================================================================\n'
printf ' Reservation board is deployed.\n\n'
printf ' URL:  %s\n\n' "$URL"
printf ' Data is saved in bucket %s, with a daily backup copy under backups/.\n' "$BUCKET"
printf ' Admin key for command-line tasks: in %s (keep it private).\n' "$STATE_FILE"
if [ "$AUTH_ON" != "true" ]; then
  printf '\n IBMid sign-in is OFF. To turn it on:\n'
  printf '   1. In IBM'"'"'s SSO tool, register redirect URL:  %s/auth/callback\n' "$URL"
  printf '   2. Put client_id, client_secret and "enabled": true in %s\n' "$CONFIG_FILE"
  printf '   3. Run this script again.\n'
fi
printf '\n Logs:  ibmcloud ce app logs --name %s --follow\n' "$APP"
printf '======================================================================\n'
