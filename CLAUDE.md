# Working in this repository

Home Assistant custom integration for **Dragonfly Shipping**
(dragonflyshipping.nl, an Intelcom brand) NL parcel tracking. Distributed via
HACS; not part of HA core. Fifth carrier in the suite — same canonical shape,
events and entity set; **bootstrapped from ha-gls** (the other account-less
carrier), **mirror GLS when in doubt**. No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change first-refresh or unmapped-status logging | *Parcel contract* (this repo implements it; below is only where Dragonfly deviates) |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` cleanly.
- **Setup cleanup filters `domain == "sensor"` and excludes
  `non_parcel_unique_ids`** — else it deletes the button / diagnostic sensors.
  Per-parcel sensors are removed via the entity registry (self-removal races).

## The API (reverse-engineered from the consumer site)

- **Endpoint** `GET https://dragonflyshipping.nl/cfworker/v3/tracking/{code}/` —
  the Cloudflare worker the site's tracking page calls. No auth, keyed on the
  tracking code alone — **no postal code** (the big divergence from GLS).
- **Always HTTP 200** with a JSON envelope: `{"success":true,"data":{"result":
  {...}}}` on a hit; `{"success":false,"data":{"status":404,"code":"not_found",
  ...}}` for unknown/not-yet-scanned → `None` (like GLS's 204). Any other failure
  envelope raises `DragonflyApiError`.
- **Result fields consumed** (verified against the site bundle
  `itc.min.<version>.js`):
  - `tracking_id` → canonical `barcode`.
  - `last_status.step` drives the 4-segment progress bar: 1 registered, 2 in
    transit, 3 out for delivery, 4 delivered; a **negative** step is the site's
    exception state → `problem`. Unmapped non-negative → `unknown` + one-shot
    WARNING (`_unmapped_steps_logged`). `status`/`statusCode` are numeric but
    mapping stays **step-based** on purpose.
  - `last_status.isDelivered` (authoritative; `step == 4` fallback), `.timestamp`
    (`delivered_at`), `.task_type` (`last_mile_pickup` = driver-comes-to-you →
    `pickup: true`; Dragonfly has **no parcel-shop network**, so `at_pickup_point`
    never occurs and the GLS `en_route_to_parcel_shop`/`awaiting_pickup` sensors
    were dropped).
  - ETA gating: the site hides the ETA when `!showEta || etaType == "none"`;
    `normalize_parcel` gates `planned_from/to` the same way. `public_eta.from/.to`
    is the window. **Live-verified caveat (2026-07):** a real out-for-delivery
    parcel had `public_eta: null` while top-level `eta` / `buffered_eta` held the
    estimate — those are the fallback; a `buffered_eta == eta` is a point estimate
    → `planned_to: None`.
  - **Timestamps are epoch milliseconds** on `last_status.timestamp` and every
    `status_list[].timestamp`; ETA fields are ISO strings. `_to_iso_timestamp`
    normalises both to ISO — do not feed raw API timestamps into
    `delivered_at`/history.
  - `status_list[]` is the history timeline (`{step, timestamp, labels}`) in the
    same response, so opt-in history costs no extra request.
  - **Labels**: `labels.shortLabel.{nl,en}` (new) or `shortLabel.{nl,en}`
    (legacy), checked in that order (`status_label()`); Dutch preferred, English
    fallback. `[link url]text[/link]` markup is stripped to inner text; `{token}`
    placeholders filled from `package_location.address`, unknown tokens stay
    literal.
  - `client_code` → `sender` (best available signal); `driver_name` stays in
    `raw`. `weight`/`dimensions` always `None` (not provided).
  - Tracking-code normalisation mirrors the site: uppercase, strip non-`A-Z0-9`
    (`normalize_tracking_code`); validation `^[A-Z0-9]{6,30}$`.

## Hub model: single instance, zero-input setup

- **`single_config_entry: true`** — with no account and no postcode there's
  nothing to key multiple hubs on. `async_step_user` creates the entry
  immediately with empty `CONF_PARCELS` (no form, no API call); a second flow
  aborts with HA's `single_instance_allowed`. `unique_id = DOMAIN` is
  belt-and-braces.
- **Tracked parcels in `entry.options[CONF_PARCELS]`** as `{tracking_code}` dicts
  (dicts, not strings, so future per-parcel fields need no migration). Added three
  ways (options flow, `dragonfly.track_parcel`/`untrack_parcel` services — no
  postal-code field, unlike GLS, a Lovelace button). Options flow is one sectioned
  form (`parcels`/`delivered`/`history`/`polling`), remove-then-add order.
- **Option changes apply live, no reload** — update listener retunes
  `coordinator.update_interval` + `async_request_refresh()`; do NOT switch to
  `async_schedule_reload`. Services are removed on unload unconditionally (single
  instance — no other-hubs check needed, unlike GLS).

## Coordinator (mirror GLS, adapted)

Concurrent per-parcel `asyncio.gather`; `_raw_cache` keyed on tracking code
(transient error / `not_found` blip keeps the last good payload; a first-ever
`not_found` yields the pending placeholder `{"tracking_id":code,"last_status":
None}` → `unknown`); `UpdateFailed` only when every fetch errored and nothing is
cached; delivered-retention filter; `last_success_time` stamped only on a real
success. Four change events (`registered`/`status_changed`/`delivered`/
`delivery_time_changed`) with cached `device_id` and first-refresh suppression,
over active+delivered combined (terminal hop → only `_delivered`). One
Dragonfly-specific bit: `result.setdefault("tracking_id", code)` after a fetch so
an edge payload without the field keeps its sensor key.

## Entities

`sensor` (incoming summary + per-parcel + next_delivery + delivered_parcels +
diagnostic `last_update`), `button` (refresh), `calendar` (deliveries, read-only,
enabled by default), device triggers. **No pickup-point sensors** (see `task_type`
above).

## Tests on Windows

`tests/conftest.py` carries two Windows-only shims (no-ops elsewhere):
`disable_socket` is neutralised (Windows event loops need AF_INET socketpairs;
the 127.0.0.1 allowlist stays) and HA's `AsyncResolver` is swapped for
`ThreadedResolver` (aiodns refuses the Proactor loop). Do not remove them
"because CI passes" — CI is Linux, development is Windows.

## Running tests

```
python -m pytest tests/ --cov=custom_components.dragonfly
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. README stays lean/installer-first (device triggers folded into
**Events**); this file documents everything. This repo is what the
ha-carrier-template was originally extracted from.
