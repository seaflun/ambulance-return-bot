---
name: nas-line-push
description: Use when a Synology NAS Docker Compose, Selenium, DSM Task Scheduler, or other hosted automation needs LINE Messaging API success or failure notifications.
---

# NAS LINE Push

## Overview

This skill packages the working pattern for Synology-hosted automation jobs that run in Docker and notify one or more LINE users when the job succeeds or fails. Use it to set up a new NAS job, repair an unstable one, or add LINE push notifications to an existing Python or Selenium workflow.

## Ambulance Return Boundary

When this skill is used inside `ambulance_return_bot` or 救護返隊小幫手, the repository `AGENTS.md` takes priority: NAS runs the Flask task center only. Keep Chrome, Selenium, worker GUI, case lookup, and four-site entry on the public-duty Windows PC. Do not add a NAS Selenium service to that project.

## Workflow

1. Inspect the existing automation first.
   Check for `compose*.yml`, `.env`, Python entrypoints, Selenium usage, and any existing LINE code before proposing changes.
2. Prefer Synology-safe Docker Compose.
   For DSM, prefer `image:` plus bind-mounted project files over `build:` when the UI or context path is unreliable. Keep commands single-line when DSM misparses multiline YAML.
3. Stabilize browser automation before adding notifications.
   If the job uses Selenium, make the browser session reliable first: remote readiness checks, conservative timeouts, immediate log flushing, and artifact capture.
4. Add LINE Messaging API push last.
   Use `LINE_CHANNEL_ACCESS_TOKEN` plus one or more recipient IDs from `.env`. Do not use LINE Notify.
5. Verify end-to-end.
   Confirm the job runs on NAS, artifacts are written, and the LINE push API returns `200`.

## Synology Pattern

For Synology Container Manager projects other than `ambulance_return_bot`, prefer this shape unless the repo already uses something else. This Selenium service is not allowed in the ambulance-return project; follow the boundary above there.

```yaml
services:
  selenium:
    image: ${SELENIUM_IMAGE}
    shm_size: "4gb"
    environment:
      TZ: Asia/Taipei
      SE_NODE_SESSION_TIMEOUT: "300"
      SE_SESSION_REQUEST_TIMEOUT: "300"
      SE_SESSION_RETRY_INTERVAL: "5"
      SE_START_XVFB: "false"

  app:
    image: python:3.12-slim
    depends_on:
      - selenium
    working_dir: /app
    environment:
      TZ: Asia/Taipei
      SELENIUM_REMOTE_URL: "http://selenium:4444/wd/hub"
    volumes:
      - ./:/app
    command: sh -c "pip install --no-cache-dir -r requirements.txt && python -u automation.py"
    restart: "no"
```

Use `python -u` so DSM logs flush immediately. If Selenium startup is flaky, wait on `http://selenium:4444/status` before creating the session.

## Environment Variables

Store secrets in `.env` at the project root. Typical fields:

```dotenv
SELENIUM_IMAGE=selenium/standalone-chromium:<tested-version-tag-or-digest>
PPE_ACCOUNT=...
PPE_PASSWORD=...
SELENIUM_TIMEOUT_SECONDS=60
SELENIUM_REMOTE_READY_TIMEOUT_SECONDS=180
LINE_CHANNEL_ACCESS_TOKEN=...
LINE_TO_USER_IDS=Uxxxxxxxx,Uyyyyyyyy
```

Rules:

- Pin `SELENIUM_IMAGE` to an explicitly tested version tag or digest; do not use the mutable `latest` tag.
- Prefer `LINE_TO_USER_IDS` for multiple recipients.
- Keep support for a legacy single-recipient `LINE_TO_USER_ID` only if the script already uses it.
- Treat channel access tokens, passwords, and user IDs as secrets; prefer `.env` over hardcoding.

## LINE Push Rules

Use LINE Messaging API `push` messages, not LINE Notify. The minimum payload shape is:

```json
{
  "to": "Uxxxxxxxx",
  "messages": [
    { "type": "text", "text": "Job completed" }
  ]
}
```

Implementation guidance:

- Send a success message after the main job and artifact capture complete.
- Send a failure message from the main exception handler, but do not let LINE failures hide the original automation error.
- Log per-recipient status such as `[line] push sent to <userId>: 200`.
- Cap the text length to the Messaging API text limit.

## Validation

When changing a NAS + LINE automation, validate in this order:

1. Local syntax check for Python or shell entrypoints.
2. Compose sanity check and environment variable coverage.
3. NAS run log shows Selenium readiness, login start, and end-state markers.
4. Output artifacts update timestamps.
5. LINE push returns `200`.

For Synology troubleshooting, classify failures quickly:

- If only `pip` output appears, the command string or log flushing is wrong.
- If Selenium is ready but session creation fails, treat it as container/browser startup instability.
- If browser reaches `opening login page` and stalls, reduce page-load waiting and rely on DOM element waits.
- If artifacts exist but no LINE arrives, test the push endpoint separately with the same token and user IDs.

## Output Expectations

When using this skill, produce:

- A concrete `.env` contract listing required secrets.
- A Synology-safe Compose or scheduled command.
- Any script changes needed for reliability and LINE push.
- A short verification checklist the user can run in DSM: job log, artifact timestamp, LINE receipt.
