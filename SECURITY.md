# Security notes

Pi OJ executes submitted programs and exposes optional external integrations.
Run it only on a trusted network unless the deployment has been protected by
an authenticated reverse proxy or a private tunnel.

## Deployment checklist

- Copy `.env.example` to `.env`; never commit `.env`, API keys, or judge
  credentials.
- Keep `backend/sandboxes` on a local filesystem with restrictive permissions.
- Review CPU, memory, timeout, and output limits before exposing the service.
- Keep the Docker socket available only to the worker service. The worker runs
  as root because it launches judge containers; do not expose that service
  directly to the network.
- Prefer binding the web port to `127.0.0.1` when using ngrok or another
  reverse tunnel.
- Use `ngrok-policy.yaml` or an equivalent access-control layer for private
  deployments.
- Rotate credentials if `.env` or a backup containing `.env` is ever exposed.

## Sandbox boundary

The judge is intended for controlled local execution, not hostile multi-tenant
isolation. Review the judge Dockerfile and `backend/tasks.py` before accepting
untrusted submissions. Keep network access disabled for judge containers where
possible, and treat output files, compiler errors, and downloaded problem data
as untrusted input.

## Reporting

Do not publish credentials, private tunnel URLs, or user submissions in an
issue. Report reproducible security problems privately to the repository owner
with the affected component, configuration, reproduction steps, and impact.
