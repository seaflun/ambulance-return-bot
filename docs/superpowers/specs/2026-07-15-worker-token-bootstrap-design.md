# Worker Token Bootstrap Design

**Status:** Approved by the user on 2026-07-15.

## Goal

Allow a public-duty Worker that can reach only one built-in NAS route on its first start to safely become eligible for remote commands, without trusting a manual URL or accepting a changed NAS instance.

## Root Cause

The route chooser deliberately labels a single reachable route `unverified` when no locally persisted NAS instance UUID exists.  The Worker still sends a heartbeat, but the NAS correctly refuses command claims from that route.  The initial `/worker/identity` request and later `/worker/control` request both use the Worker Token, but the old code had no narrow path for recording that first authenticated identity.

## Chosen Design

`WorkerControlClient` may bootstrap trust only after a successful `POST /worker/control` response when all of these conditions hold:

1. The Worker started from an exact built-in NAS LAN or Tailscale URL and retains immutable `builtin` provenance.  `RouteChoice` and environment provenance default to `manual`; a missing or invalid value is never inferred from URL text.  A URL entered or changed through the GUI is `manual` even when its text equals a built-in URL.
2. The route is the first-start single-route state: `identity_status == "unverified"`, a UUID from `/worker/identity` is present, the diagnostic is exactly `single_route_unverified`, and there is no stored UUID.
3. The token-authenticated control response returns the same NAS `instance_id` as the earlier token-authenticated identity request.
4. A strict compare-and-set helper obtains a short cross-process sidecar lock, accepts only a missing cache or an identical UUID, and atomically writes the UUID.  A malformed cache, lock failure, write failure, or different UUID fails closed and is never overwritten.

The client snapshots the actual endpoint used for every control request in an immutable `RequestRouteSnapshot` containing `url`, route name, identity status, instance ID, and provenance.  It takes this snapshot before each POST and replaces it with the actual Tailscale endpoint if verified LAN transport fallback is used.  This snapshot is an attribute of a mapping-compatible client response, not NAS JSON data.  The response that performs bootstrap still carries an unverified route; any command in such a response is discarded using that snapshot before the client changes its in-memory route to `verified`.  A plain or invalid response without a client snapshot also discards its command.  Only the next 10-second control request carries `verified`, so the existing NAS command-claim guard remains authoritative.

## Refusal Rules

- A manual URL is never eligible for bootstrap.  The gate also requires the canonical built-in primary URL, its matching route name, and an empty fallback URL.
- A known local UUID that differs from the only reachable NAS UUID stays unverified; the chooser emits a distinct `single_route_known_instance_mismatch` diagnostic.
- Any non-empty expected UUID that differs from the control response raises an identity-mismatch error and is never persisted.
- The GUI does not write the identity cache after a route probe; only a successful control-response path may use the compare-and-set helper.
- Unverified routes never use fallback.  Verified fallback keeps its existing transport-error-only rule.
- No NAS API, Worker Token format, heartbeat schema, or remote-update ownership rule changes.

The existing project assumes the LAN/Tailscale Worker Token transport is trusted; this change does not add TLS or certificate pinning.  Protecting against an active network attacker is a separate NAS transport-security project, not a reason to weaken the current command guard.

## Test and Live Acceptance

Regression tests prove promotion for the pinned first-start case, request-route snapshot rejection of the first response command, actual-Tailscale fallback metadata, rejection for manual (including a manual built-in-looking string) and mismatch cases, non-promotion after a known-ID mismatch, strict cache compare-and-set behavior across processes, and correct GUI/Worker builder opt-in.  They also prove that headless Worker construction defaults to manual/read-only unless an explicit provenance value is supplied.  Release acceptance requires a fresh public-duty heartbeat with `route.identity_status == "verified"`, followed by a real remote-update command reaching a terminal `up_to_date` or `completed` state.
