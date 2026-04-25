# Voxtera Runbook

> Status: stub. Operational procedures will be filled in once VOX-E6 (DigitalOcean deployment) is in flight.

## Scope

Day-to-day operational procedures: deploys, rollbacks, log access, key rotation, incident response, on-call playbook.

## Sections to write (each becomes a heading once we have the relevant infra)

- Deploying a new version
- Rolling back a deploy
- Accessing production logs
- Rotating API keys (Anthropic, OpenAI, Daily.co, Google, Twilio)
- Responding to a CI failure on `main`
- Responding to a production error spike
- Adding a new developer (access checklist)

## Until we have production

For local-only Sprint 1 work, the only "ops" tasks are:

- Rotate a leaked key: revoke at the provider, generate a new one, update the team password manager and your local `.env`. Then notify the team.
- Recover from a broken `main`: revert the offending commit on a new branch and open a PR; do not force-push to `main`.
