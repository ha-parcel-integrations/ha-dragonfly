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
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change first-refresh or unmapped-status logging | *Parcel contract* (this repo implements it; below is only integration-level deviations) |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**API mechanics live in `docs/api/` (local-only, gitignored)** — the endpoint,
response envelope, payload→canonical mapping, the step/status vocabulary and the
timestamp formats. Do not duplicate them here; this file is HA-integration
decisions only.

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` cleanly.
- **Setup cleanup filters `domain == "sensor"` and excludes
  `non_parcel_unique_ids`** — else it deletes the button / diagnostic sensors.
  Per-parcel sensors are removed via the entity registry (self-removal races).

## Hub model: single instance, zero-input setup

- **`single_config_entry: true`** — with no account and no postcode there's
  nothing to key multiple hubs on. `async_step_user` creates the entry
  immediately with empty `CONF_PARCELS` (no form, no API call); a second flow
  aborts with HA's `single_instance_allowed`. `unique_id = DOMAIN` is
  belt-and-braces.
- **Tracked parcels in `entry.options[CONF_PARCELS]`** as `{tracking_code}` dicts
  (dicts, not strings, so future per-parcel fields need no migration). Added three
  ways (options flow, `dragonfly.track_parcel`/`untrack_parcel` services — no
  postal-code field, unlike GLS — and a Lovelace button). Options flow is one
  sectioned form (`parcels`/`delivered`/`history`/`polling`), remove-then-add
  order.
- **Option changes apply live, no reload** — update listener retunes
  `coordinator.update_interval` + `async_request_refresh()`; do NOT switch to
  `async_schedule_reload`. Services are removed on unload unconditionally (single
  instance — no other-hubs check needed, unlike GLS).

## Coordinator behaviour (mirror GLS, adapted)

Concurrent per-parcel `asyncio.gather`; **`_raw_cache`** keyed on tracking code so
a transient error or an unknown-code blip keeps the last good payload, and a
first-ever unknown code yields a pending placeholder (status `unknown`) rather
than dropping the parcel; **`UpdateFailed` only when every fetch errored and
nothing is cached**; delivered-retention filter (display-only); **`last_success_time`
stamped only on a real success** (a poll served entirely from cache is not one —
the diagnostic sensor exists to reveal that). Four change events
(`registered`/`status_changed`/`delivered`/`delivery_time_changed`) with cached
`device_id` and first-refresh suppression, over active+delivered combined
(terminal hop → only `_delivered`). History is opt-in, default off, and costs no
extra request (it ships in the same response).

## Entities

`sensor` (incoming summary + per-parcel + next_delivery + delivered_parcels +
diagnostic `last_update`), `button` (refresh), `calendar` (deliveries, read-only,
enabled by default), device triggers. **No pickup-point sensors** — Dragonfly has
no parcel-shop network, so `at_pickup_point` never occurs and GLS's
`en_route_to_parcel_shop` / `awaiting_pickup` sensors were dropped.

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
**Events**); this file documents integration decisions, `docs/api/` documents the
API. This repo is what the ha-carrier-template was originally extracted from.
