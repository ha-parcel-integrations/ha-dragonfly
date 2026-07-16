# Working in this repository

This is a Home Assistant custom integration for **Dragonfly Shipping**
(dragonflyshipping.nl, an Intelcom brand) parcel tracking in the Netherlands.
Distributed via HACS; not part of HA core. It is the fifth carrier in the
parcel suite (alongside DHL, DPD, PostNL, GLS) and follows the same canonical
shape, events and entity set — it was bootstrapped from **ha-gls** (the other
account-less carrier); mirror GLS when in doubt.

## Always consult HA developer documentation

Home Assistant's integration patterns evolve. **Do not rely on memory** —
fetch the canonical page before changing a topic area, and check the
developer blog before introducing anything you only "know" from training.

| When you change | Fetch first |
|---|---|
| Entity properties, naming, lifecycle, attributes | https://developers.home-assistant.io/docs/core/entity/ |
| Config flow, options flow | https://developers.home-assistant.io/docs/config_entries_config_flow_handler |
| DataUpdateCoordinator | https://developers.home-assistant.io/docs/integration_fetching_data |
| Quality scale rules | https://developers.home-assistant.io/docs/core/integration-quality-scale |

## The API (reverse-engineered from the consumer site)

- **Endpoint** (`TRACKING_API_URL` in `const.py`):
  `GET https://dragonflyshipping.nl/cfworker/v3/tracking/{code}/` — the same
  Cloudflare worker the site's tracking page calls (`data-url` on the
  `.js-tracking` element). No auth, keyed on the tracking code alone —
  **no postal code**, which is the big divergence from GLS.
- **Always HTTP 200** with a JSON envelope: `{"success": true, "data":
  {"result": {...}}}` on a hit; `{"success": false, "data": {"status": 404,
  "code": "not_found", ...}}` for an unknown / not-yet-scanned code (that maps
  to `None` in the client, like GLS's 204). Any other failure envelope raises
  `DragonflyApiError`.
- **Result fields we consume** (verified against the site bundle
  `itc.min.<version>.js`, components `tracking-api` / `tracking-success` /
  `tracking-history` / `tracking-eta` / `tracking-progress`):
  - `tracking_id` — the code (canonical `barcode`).
  - `last_status.step` — drives the site's 4-segment progress bar:
    1 registered, 2 in transit, 3 out for delivery, 4 delivered; a
    **negative** step is the site's exception state (painted red) → mapped to
    `problem`. Unmapped non-negative steps → `unknown` + one-shot WARNING with
    the issues link (`_unmapped_steps_logged`).
  - `last_status.isDelivered` (authoritative delivered flag; `step == 4` is
    the fallback), `.timestamp` (used as `delivered_at`), `.task_type`
    (`last_mile_pickup` = driver-comes-to-you task → `pickup: true`; Dragonfly
    has **no parcel-shop network**, so `at_pickup_point` never occurs and the
    GLS `en_route_to_parcel_shop` / `awaiting_pickup` sensors were dropped).
  - `last_status.showEta` + `.etaType` — the site hides the ETA when
    `!showEta || etaType == "none"`; `normalize_parcel` gates
    `planned_from/to` the same way.
  - `public_eta.from` / `.to` — the delivery window (`planned_from/to`).
  - `status_list[]` — the history timeline (`{step, timestamp, labels}`),
    ships in the same response, so the opt-in history costs no extra request.
  - **Labels**: every status object embeds localized texts —
    `labels.shortLabel.{nl,en}` on new payloads, `shortLabel.{nl,en}` directly
    on legacy ones (the site checks both, in that order — `status_label()`
    mirrors it). Dutch preferred, English fallback (`LABEL_LANGUAGES`).
    `[link url]text[/link]` markup is stripped to the inner text; `{token}`
    placeholders are filled from `package_location.address` when present,
    unknown tokens stay literal.
  - `client_code` is exposed as `sender` (best available sender signal);
    `driver_name` stays in `raw` only.
- `weight` / `dimensions` are always `None` — the API does not provide them.
- Tracking-code normalisation mirrors the site's sanitiser: uppercase, strip
  everything not `A-Z0-9` (`normalize_tracking_code`); validation is
  `^[A-Z0-9]{6,30}$`.

## The hub model: single instance, zero-input setup

- **`single_config_entry: true`** in the manifest — with no account and no
  postal code there is nothing to key multiple hubs on. `async_step_user`
  creates the entry immediately with empty `CONF_PARCELS` (no form, no API
  call). A second flow aborts with HA's own `single_instance_allowed` (before
  our code runs); the `unique_id = DOMAIN` guard is belt-and-braces.
- **Tracked parcels live in `entry.options[CONF_PARCELS]`** as
  `{tracking_code}` dicts (dicts, not strings, so future per-parcel fields
  need no migration). Added three ways, all validated the same: the options
  flow, the `dragonfly.track_parcel` / `untrack_parcel` services
  (`services.py` — no postal-code field, unlike GLS), and a Lovelace button
  calling the service.
- **Options flow = one sectioned form** (`parcels` / `delivered` / `history`
  / `polling`), remove-then-add order so re-adding a just-removed code works.
- **Option changes apply live, no reload** — update listener retunes
  `coordinator.update_interval` + `async_request_refresh()`; do NOT switch to
  `async_schedule_reload` (see GLS CLAUDE.md for the full rationale).
- Services are removed on unload unconditionally (single instance — no
  other-hubs-still-loaded check needed, unlike GLS).

## Coordinator (mirror GLS, adapted)

Same architecture as GLS: concurrent per-parcel `asyncio.gather`, `_raw_cache`
keyed on tracking code (transient error / `not_found` blip keeps the last good
payload; a first-ever `not_found` yields the pending placeholder
`{"tracking_id": code, "last_status": None}` → status `unknown`),
`UpdateFailed` only when every fetch errored and nothing is cached,
delivered-retention filter, `last_success_time` only stamped on a real
success, first refresh in `__init__.py` before `async_forward_entry_setups`,
and the three change events with cached `device_id` and first-refresh
suppression. One Dragonfly-specific addition: `result.setdefault("tracking_id",
code)` after a fetch, so an edge payload without the field keeps its sensor
key.

## Entities

`sensor` (incoming summary + per-parcel + next_delivery + delivered_parcels +
diagnostic `last_update`), `button` (refresh), `calendar` (deliveries,
read-only, enabled by default), device triggers. **No pickup-point sensors**
(see task_type note above). The setup-time stale-entity cleanup in `sensor.py`
is scoped to `entity_entry.domain == "sensor"` and excludes the
summary/diagnostic unique_ids — do not drop either guard.

## Tests on Windows

`tests/conftest.py` carries two Windows-only shims (both no-ops elsewhere):
pytest-homeassistant-custom-component's `disable_socket` is neutralised
(Windows event loops need AF_INET socketpairs; the connect-time
127.0.0.1 allowlist stays), and HA's hardcoded aiohttp `AsyncResolver` is
swapped for `ThreadedResolver` (aiodns refuses the Proactor loop). Do not
remove them "because CI passes" — CI is Linux, development happens on Windows.

## Docs / README

The README stays **lean, installer-first** (suite house style): no
`## Buttons` / `## Calendar` sections; the device-trigger option is one
sentence folded into **Events**. CLAUDE.md documents everything.

## Running tests

```
python -m pytest tests/ --cov=custom_components.dragonfly
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing.
