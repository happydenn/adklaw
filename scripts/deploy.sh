#!/usr/bin/env bash
# Deploy adklaw to Cloud Run (the agent runs as an HTTP service via
# `app/fast_api_app.py`, fronted by ADK's `get_fast_api_app`).
#
# This script is a *template*: it wraps `gcloud run deploy --source`
# and pipes runtime env vars into the deployed service. Operators
# fill in the placeholders via env vars at run time. No project /
# region / bucket names are committed to the repo.
#
# Required env vars:
#   GOOGLE_CLOUD_PROJECT       — target GCP project
#
# Optional env vars (deploy-time):
#   GOOGLE_CLOUD_LOCATION      — region. default: us-east1
#   ADKLAW_SERVICE_NAME        — Cloud Run service name. default: adklaw-agent
#
# Optional env vars (passed through to the running container):
#   ADKLAW_KNOWLEDGE_BACKEND               — default: firestore (production)
#   ADKLAW_KNOWLEDGE_FIRESTORE_COLLECTION  — default: adklaw_knowledge
#   ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT     — default: $GOOGLE_CLOUD_PROJECT
#   LOGS_BUCKET_NAME                       — GCS bucket for ADK artifacts (existing)
#   ALLOW_ORIGINS                          — CORS allow-list (existing)
#
# Usage:
#   GOOGLE_CLOUD_PROJECT=my-proj bash scripts/deploy.sh
#
# IAM the deployed service account needs:
#   roles/datastore.user      on $GOOGLE_CLOUD_PROJECT (Firestore knowledge)
#   roles/storage.objectAdmin on the artifacts bucket (if LOGS_BUCKET_NAME set)
# See `docs/deployments.md` for the gcloud commands.
#
# Note on naming: this is a Cloud Run deployment, not a native
# Vertex AI Agent Engine deployment. ADK serves the agent via
# FastAPI and Cloud Run hosts the container. Vertex AI Agent
# Engine deployments (via the Vertex AI SDK) are out of scope
# for this script; the same workspace-bake and runtime-env-var
# patterns apply, only the deploy command differs.

set -euo pipefail

require_env() {
    local var=$1
    if [ -z "${!var:-}" ]; then
        echo "Missing required env var: $var" >&2
        echo "See header of $0 for the full env var contract." >&2
        exit 1
    fi
}

require_env GOOGLE_CLOUD_PROJECT

LOCATION="${GOOGLE_CLOUD_LOCATION:-us-east1}"
SERVICE_NAME="${ADKLAW_SERVICE_NAME:-adklaw-agent}"

# Build the comma-separated --set-env-vars payload. Each entry is
# `KEY=VALUE`. Values must not contain commas; if they ever do,
# switch to `--set-env-vars-file` (YAML).
ENV_PAIRS=(
    "GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT}"
    "ADKLAW_KNOWLEDGE_BACKEND=${ADKLAW_KNOWLEDGE_BACKEND:-firestore}"
    "ADKLAW_KNOWLEDGE_FIRESTORE_COLLECTION=${ADKLAW_KNOWLEDGE_FIRESTORE_COLLECTION:-adklaw_knowledge}"
    "ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT=${ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT:-${GOOGLE_CLOUD_PROJECT}}"
)
if [ -n "${LOGS_BUCKET_NAME:-}" ]; then
    ENV_PAIRS+=("LOGS_BUCKET_NAME=${LOGS_BUCKET_NAME}")
fi
if [ -n "${ALLOW_ORIGINS:-}" ]; then
    ENV_PAIRS+=("ALLOW_ORIGINS=${ALLOW_ORIGINS}")
fi

# Join with commas.
ENV_VARS=$(IFS=,; echo "${ENV_PAIRS[*]}")

echo "Deploying $SERVICE_NAME to $LOCATION in $GOOGLE_CLOUD_PROJECT..."
echo "Runtime env:"
for pair in "${ENV_PAIRS[@]}"; do echo "  $pair"; done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

gcloud run deploy "$SERVICE_NAME" \
    --source "$REPO_ROOT" \
    --region "$LOCATION" \
    --project "$GOOGLE_CLOUD_PROJECT" \
    --set-env-vars "$ENV_VARS" \
    --port 8080 \
    "$@"
