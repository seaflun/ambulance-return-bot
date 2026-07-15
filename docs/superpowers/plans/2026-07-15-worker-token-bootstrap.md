# Worker Token Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely establish the first pinned NAS identity after a token-authenticated Worker control response, so a single reachable built-in route can receive later remote commands.

**Architecture:** Keep route selection fail-closed.  GUI route provenance distinguishes package-selected built-in endpoints from text entered by an operator.  A control client snapshots each request route, validates the response UUID, uses a short cross-process strict CAS cache promotion, and only then changes its own in-memory route state; the next control interval uses the existing verified-command guard.

**Tech Stack:** Python 3, unittest, existing JSON atomic writer, existing Worker Token HTTP helpers.

## Global Constraints

- Only an exact built-in LAN/Tailscale endpoint with explicit `builtin` provenance may opt in; the provenance field defaults to `manual` and is never inferred from URL text.  A manual URL must remain unverified even when its text matches a built-in endpoint.
- Persist a UUID only after the Worker Token authenticated control response matches the token-authenticated identity UUID, through a strict missing-or-same cross-process CAS.
- The immutable actual-endpoint request snapshot determines mailbox eligibility; a plain response without a valid client snapshot or an unverified response command must never become executable after promotion.
- A known UUID mismatch, malformed cache, lock/write failure, response mismatch, unverified fallback, and NAS command-claim guard must remain fail-closed.
- Touch only the Worker route/control/GUI builder and focused tests; do not alter NAS API or Worker Token handling.

---

### Task 1: Make control-response promotion and its identity cache fail closed

**Files:**

- Modify: `WinPython_公務電腦使用包/ambulance_bot/worker_routes.py:37-103,125-145,250-258`
- Modify: `WinPython_公務電腦使用包/ambulance_bot/worker_control.py:141-172,262-285`
- Test: `tests/test_worker_routes.py:70-90,185-223`
- Test: `tests/test_worker_control.py:58-110`

**Interfaces:**

- `RouteChoice` retains explicit `builtin` or `manual` provenance with a safe `manual` default for existing positional callers.
- `WorkerControlClient.control(payload)` returns a mapping-compatible response carrying an immutable actual-endpoint request snapshot used for that request; it validates every non-empty expected UUID before returning.
- `try_promote_known_server_identity(instance_id)` takes the sidecar lock and accepts only a missing or same valid UUID.
- A first-start `RouteChoice` may change only from `unverified` to `verified` after the validated response and successful strict promotion.

- [ ] **Step 1: Write the failing route tests**

  Add independent tests for a matching pinned first-start route, a manual route, a mismatched control response, a single route that disagrees with an existing known UUID, strict cache rejection of malformed or different existing state, external lock timeout, write failure, and two concurrent candidate processes.  The matching case must capture that its first payload is still `unverified`, discard a simulated first-response command, then assert the client becomes `verified` only after strict identity persistence succeeds.  A verified LAN transport failure must record Tailscale as the actual snapshot endpoint.

- [ ] **Step 2: Run the new tests to verify RED**

  Run:

  ```powershell
  C:\Users\seafl\AppData\Local\Python\pythoncore-3.14-64\python.exe -m unittest tests.test_worker_routes.WorkerRouteTests.test_control_client_bootstraps_only_pinned_first_start_route -v
  ```

  Expected: FAIL because the client has no bootstrap option and does not promote the route.

- [ ] **Step 3: Implement the minimal route/client change**

  Distinguish `single_route_known_instance_mismatch` from the empty-known-ID diagnostic.  Add immutable actual-endpoint request metadata to the control response, strict sidecar-lock cache promotion, and a built-in provenance gate.  `try_promote_known_server_identity()` itself must hold the sidecar lock while strict-reading and deciding missing/same/different/malformed state.  After strict response UUID validation, persist and promote only a canonical built-in `lan` or `tailscale` choice with the empty-known-ID single-route diagnostic and empty fallback.  The control loop must discard a command whose request-route snapshot is missing or not verified.  Leave manual choices, known-ID mismatches, invalid UUIDs, response mismatches, and fallback behavior unchanged.

- [ ] **Step 4: Run focused GREEN tests**

  Run:

  ```powershell
  C:\Users\seafl\AppData\Local\Python\pythoncore-3.14-64\python.exe -m unittest tests.test_worker_routes tests.test_worker_control -v
  ```

  Expected: all pass, with the existing manual-heartbeat behavior retained.

- [ ] **Step 5: Commit the focused change**

  ```powershell
  git add -- WinPython_公務電腦使用包/ambulance_bot/worker_routes.py WinPython_公務電腦使用包/ambulance_bot/worker_control.py tests/test_worker_routes.py tests/test_worker_control.py docs/superpowers/specs/2026-07-15-worker-token-bootstrap-design.md docs/superpowers/plans/2026-07-15-worker-token-bootstrap.md
  git commit -m "fix: bootstrap pinned worker identity"
  ```

---

### Task 2: Preserve built-in route provenance through GUI and Worker construction

**Files:**

- Modify: `WinPython_公務電腦使用包/worker_gui.py:375-385,629-669,983-995,1712-1738`
- Modify: `WinPython_公務電腦使用包/worker.py:215-257`
- Test: `tests/test_worker_gui.py:531-560`
- Test: `tests/test_worker.py:1331-1393`

**Interfaces:**

- GUI startup recognizes package-configured built-in endpoints as `builtin`; editing the NAS entry marks it `manual`.
- `choose_worker_server(..., builtin_origin: bool)` marks a choice eligible only when that explicit provenance is true.
- `build_worker_control_loop()` reads a separate explicit provenance environment value.  Missing, invalid, or headless-only values are manual/read-only; it passes exact built-in endpoint data to the client only for a `builtin` route.

- [ ] **Step 1: Write failing provenance tests**

  Add tests that a default built-in route remains eligible, a custom URL is manual, a user-entered string equal to the LAN constant remains manual through GUI choice and Worker environment reconstruction, and a headless builder without explicit provenance is manual/read-only.  Assert both GUI identity-probe paths no longer call the cache writer.

- [ ] **Step 2: Run the new tests to verify RED**

  Run the focused GUI and Worker tests.  Expected: FAIL because the current GUI infers trust only from the URL text and writes the identity cache before control succeeds.

- [ ] **Step 3: Implement provenance without changing NAS APIs**

  Track origin separately from the entry text, propagate it through the `RouteChoice` and Worker environment, and remove both GUI probe-time cache writes.  The builder must give the control client no bootstrap endpoint for manual origin.

- [ ] **Step 4: Run focused GREEN tests and commit**

  Run `tests.test_worker_gui`, the two focused `tests.test_worker` control-builder cases, plus the Task 1 suites.  Commit only the GUI/Worker provenance files, their tests, and this plan/spec after reviewing the result.
