# Changelog

## Unreleased

- Verify alarm sound with both capability and readable mode, probe independent
  public 301/305 alarm-light state with exact type echo, and persist verified
  protocol evidence without identifiers or private mobile APIs.
- Add a hub-cooled per-device status refresh button and Hub-local API request
  and rate-limit counters with throttled display and manual local refresh.
- Align the bilingual README with current hub naming, shared-device selection,
  control and image availability, API access, and webhook security boundaries.
- Detect alarm-sound support from the low-frequency device-status response
  instead of relying on device-list fields that current China Open Platform
  responses may omit, while excluding shared recipients and unsupported modes.
- Keep Hub-name validation out of frontend-serialized schemas so the add and
  options flows load normally, while reporting invalid names as field errors.
- Add editable HomeKit-style Hub names, omit redundant channel labels for
  single-channel devices, retain channel labels for multi-channel devices, and
  refine user-facing entity names and icons without changing registry IDs.
- Preserve official device names while omitting a fabricated model label for
  shared or managed devices whose model is unavailable.
- Show a localized Home Assistant persistent notification containing the private
  Cloud Push webhook URL when a hub generates it for the first time.
- Add a default-enabled first-step option for shared/managed devices, preload
  those devices before include/exclude selection, and carry the setting through
  metadata, state, capability, entity, diagnostics, quota, and options refresh
  paths. Shared recipients are identified by the official `isShared=2` camera
  list value rather than generic boolean coercion.
- Add the initial Home Assistant hub integration for the official EZVIZ China Open Platform.
- Add capability-driven alarm, state, image, defence, alarm-sound, capture, and
  PTZ entities without exposing unsupported camera-stream entities.
- Add bounded polling, native webhook reconciliation, secret-safe diagnostics,
  stable registry identities, and bilingual configuration flows.
- Create motion, person, alarm, doorbell, calling, and event-image entities
  only after the device has demonstrated the corresponding event semantic.
- Implement the China Cloud Push `header`/`body` acknowledgement contract for
  `ys.alarm` and `ys.calling`, including fast HTTP 200 `messageId` responses,
  bounded background processing, and aggregate-only diagnostics.
- Parse current official Push semantics strictly: `ys.calling.status` 1/2/3,
  documented channel/time fields, typed envelope values, and per-stage call
  deduplication without legacy field guesses.
- Bound callback body reads, keep the worker alive after individual failures,
  restrict event-image downloads to public HTTPS targets without redirects,
  and remove private persisted state when a config entry is deleted.
- Query `/alarm/list` with a bounded 24-hour window and the documented 50-item
  page limit, and ship matching English and Simplified Chinese translations.
- Add bilingual webhook security guidance and conservative daily API/capture
  usage estimates to the Home Assistant options UI.
- Add a two-step setup flow with explicit include/exclude device filtering,
  shared filter semantics across metadata, state, alarms, and quota estimates,
  and legacy include-list compatibility.
- Preserve the stable native webhook URL when credentials are reauthenticated
  or reconfigured, while continuing to acknowledge valid official envelopes
  before filtering unknown or excluded targets.
- Batch device discovery during credential validation and mark the credential
  form as a non-final step so Home Assistant presents the two-step Hub setup
  correctly without one device-list request per device.
- Declare Home Assistant's `webhook` dependency so native callback registration
  cannot race the core webhook integration.
- Isolate explicit unsupported and partial connection failures during
  per-device capability discovery while keeping authentication, rate-limit,
  protocol, and total connection failures visible.
- Replace repeated full-day alarm polling with a startup 24-hour recovery and
  incremental 60-second-overlap windows that advance only after success.
- Download untrusted temporary images through a dedicated connection-time
  public-DNS-only resolver/session, eliminating the preflight DNS-rebinding
  window while retaining HTTPS, no-redirect, content-type, and 8 MiB limits.
- Remove physical serial numbers from PTZ task names and add static privacy
  guards for future task labels and log calls.
- Document official API authority and third-party CN project corroboration
  without adopting private mobile APIs or copying third-party source.
