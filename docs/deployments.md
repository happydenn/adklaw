# Deployments

How to deploy adklaw, what each deployment shape provides,
and the env-var / IAM contract the deployed service needs.

## Capability matrix

The same agent code, deployed three ways, has different
*persistence* and *external connectivity* — but the same
tools and behaviors.

| | Local dev | Cloud Run + Discord | Cloud Run (gws / HTTP) |
|---|---|---|---|
| Sessions | Sqlite (persistent) | Sqlite (persistent w/ volume) | InMemory (default) or VertexAi |
| Artifacts | InMemory | GCS | GCS |
| Knowledge | Local files | Local (volume) or Firestore | Firestore |
| Workspace | Persistent (user FS) | Persistent (volume) or ephemeral | Ephemeral (baked image) |
| Workspace tools | yes | yes | yes |
| `run_shell` | yes | yes | yes |
| Skills | yes | yes | yes |
| Memory Bank | optional | optional | optional |
| Channels | Discord, CLI | Discord | gws (Gemini Enterprise) |

> The "Cloud Run (gws / HTTP)" column today serves the agent
> via ADK's `get_fast_api_app` over HTTP. Native Vertex AI
> Agent Engine deployments via the Vertex AI SDK are out of
> scope for the current scripts; the same workspace-bake and
> runtime env-var patterns apply, only the deploy command
> differs.

## How customization survives stateless deploys

The agent's behavior is shaped by three things at runtime:

- **`BASE_INSTRUCTION`** — codebase-hardcoded. Travels with
  the image automatically.
- **Workspace `AGENTS.md` and other top-level `*.md`** — *baked
  into the image* via `Dockerfile`'s
  `COPY ./templates ./workspace`. Live edits to
  `<repo>/templates/AGENTS.md` ship in the next image build.
- **Knowledge entries** — durable in **Firestore** when the
  firestore backend is selected (the prod default in the
  deploy script). The local filesystem is ephemeral on
  Cloud Run / Agent Runtime, so storing knowledge in the
  workspace would lose it on container restart. See
  `docs/knowledge.md`.

So: edit `templates/AGENTS.md` locally, rebuild and redeploy
to roll out persona / convention changes. The agent's
*memory of facts* — what it has learned and recorded — is
independent and lives in Firestore across restarts.

## Env vars at runtime

What the deployed container reads on every turn.

| Var | Required | Default | Effect |
|---|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | yes | — | Vertex AI model auth + Firestore knowledge target. |
| `ADKLAW_KNOWLEDGE_BACKEND` | no | `local` | Set to `firestore` for stateless deploys. |
| `ADKLAW_KNOWLEDGE_FIRESTORE_COLLECTION` | no | `adklaw_knowledge` | Firestore collection name. |
| `ADKLAW_KNOWLEDGE_FIRESTORE_PROJECT` | no | falls back to `GOOGLE_CLOUD_PROJECT` | Lets you point knowledge at a different project from the model. |
| `ADKLAW_WORKSPACE` | no | set by Dockerfile to `/code/workspace` | Path to the baked-in workspace. |
| `LOGS_BUCKET_NAME` | no | unset (InMemory artifacts) | GCS bucket for ADK artifact storage. |
| `ALLOW_ORIGINS` | no | unset | CORS allow-list for the FastAPI app. |

Every variable above is documented in the relevant code comment:
`app/agent.py` for project/auth, `app/knowledge/service.py` for
knowledge backends, `app/fast_api_app.py` for `LOGS_BUCKET_NAME`
and `ALLOW_ORIGINS`.

## IAM the runtime service account needs

Apply these grants to whichever service account Cloud Run uses
for the service (default Compute Engine SA, or a dedicated SA
if you've created one):

```bash
PROJECT=...           # your GCP project id
SA="${PROJECT}-compute@developer.gserviceaccount.com"
# Or: SA="adklaw-agent@${PROJECT}.iam.gserviceaccount.com"
#     for a dedicated service account.

# Firestore knowledge backend.
gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA" \
    --role="roles/datastore.user"

# Vertex AI model access.
gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA" \
    --role="roles/aiplatform.user"

# (Optional) GCS artifacts bucket.
BUCKET="...-adklaw-artifacts"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
    --member="serviceAccount:$SA" \
    --role="roles/storage.objectAdmin"
```

Firestore must be set to **Native mode** in your project — the
backend uses document projection (`select(...)`) which Datastore
mode doesn't support. Provision once via:

```bash
gcloud firestore databases create \
    --location=$LOCATION \
    --type=firestore-native \
    --project=$PROJECT
```

## Building and deploying

`scripts/deploy.sh` wraps `gcloud run deploy --source` and pipes
runtime env vars into the deployed service. It does not commit
project / region / bucket names to the repo.

```bash
GOOGLE_CLOUD_PROJECT=my-proj \
GOOGLE_CLOUD_LOCATION=us-east1 \
LOGS_BUCKET_NAME=my-proj-adklaw-artifacts \
bash scripts/deploy.sh
```

The script:

1. Validates `GOOGLE_CLOUD_PROJECT` is set (fail-fast).
2. Defaults region to `us-east1`, service name to
   `adklaw-agent`, knowledge backend to `firestore`.
3. Invokes `gcloud run deploy --source <repo-root>` which
   triggers Cloud Build to build the image from the
   Dockerfile and deploy it to Cloud Run.
4. Passes runtime env vars via `--set-env-vars`.

Read the script header for the full env-var contract; it's the
authoritative source of which vars do what.

## Workspace baking

`templates/` is the source of truth for the deployed
workspace. The Dockerfile does `COPY ./templates ./workspace`,
producing `/code/workspace/AGENTS.md` (and any other `*.md`
files you've added there) inside the image. At runtime
`ADKLAW_WORKSPACE=/code/workspace` makes the agent read from
that baked-in directory.

To change persona / project conventions in production:

1. Edit `templates/AGENTS.md` locally (or add new top-level
   `*.md` files).
2. Run `bash scripts/deploy.sh` (with required env vars).
3. New build, new image, rolling update on Cloud Run.

Ephemeral writes the agent makes to its workspace at runtime —
e.g., scratch files, temporary outputs from `run_shell` — do
not persist across container restarts. Anything the agent
should *remember* belongs in Firestore knowledge
(`write_knowledge`).

`.knowledge/` is intentionally not baked into the image. In
production the firestore backend is the source of truth for
knowledge; the local-filesystem `.knowledge/` exists only when
running with `ADKLAW_KNOWLEDGE_BACKEND=local` (dev / Cloud Run
with a persistent volume).

## Registering with Gemini Enterprise

The deploy command above produces a Cloud Run service that
serves the agent over HTTP. Registering that endpoint with
Gemini Enterprise (so it shows up to Workspace users) is a
**post-deploy human action**:

1. Verify the service is up: `gcloud run services describe
   adklaw-agent --region us-east1 --format='value(status.url)'`
   should print an HTTPS URL.
2. In the Cloud Console, navigate to your Gemini Enterprise
   Agent Platform configuration and register the service URL
   as an agent endpoint.
3. Configure auth and access policies per your org's rules.

This step is left as a console action because it's a one-time
configuration per deployment and the relevant UI / IAM model
is org-specific. The deploy script intentionally stops at
"reachable Cloud Run service."

## Discord deployment is separate

The Discord channel runs as `python -m app.channels.discord`,
typically on Cloud Run with `min-instances=1` so the
`SqliteSessionService` survives. That deployment is unrelated
to this script and is unaffected by changes here. Both
deployments can share the same Firestore knowledge collection
and (optionally) the same GCS artifacts bucket.

## What this deployment does *not* set up

These are intentionally out of scope:

- **Terraform / repeatable infrastructure.** The project isn't
  scaffolded with `agents-cli infra`, so deployment is plain
  `gcloud` for now. Move to Terraform if/when scale demands.
- **Secret Manager wiring.** No secrets are wired today
  because no secrets are needed at deploy time. If
  Discord-style channels grow that need them, switch to
  `--set-secrets` on the deploy command.
- **CI/CD.** This is a local-operator script. Hooking into
  GitHub Actions or Cloud Build triggers is a follow-up.
- **Native Vertex AI Agent Engine deployment.** Today's setup
  uses Cloud Run + ADK FastAPI. If `gcloud beta ai
  agent-engines deploy` becomes the target later, the
  Dockerfile bake and the runtime env vars carry over; the
  deploy command is the only thing that changes.
