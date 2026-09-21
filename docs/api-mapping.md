# Official API Mapping

This document maps production behavior to the public EZVIZ China Open Platform
surface used by the integration. The client base URL is:

`https://open.ys7.com/api/lapp`

All listed LAPP operations use HTTPS `POST`. The Home Assistant API client is
independent from entity code and receives the shared `aiohttp` session. Image
URLs returned by the platform use a separate integration-owned session because
their connection resolver has a stricter trust boundary.

## Endpoint map

| Path | Integration use | Retry policy | Data consumer |
| --- | --- | --- | --- |
| `/token/get` | Exchange AppKey/AppSecret for a bounded access token | No blind retry; one synchronized refresh | API client |
| `/device/list` | Paginated device state and configuration | Bounded read retry | Metadata/state coordinators |
| `/device/status/get` | Read the current alarm-sound mode for a directly controlled device | Bounded read retry; unsupported and connection failures are device-scoped | Metadata coordinator |
| `/camera/list` | Paginated owned/shared channels, online state, and encryption metadata | Bounded read retry | Setup discovery and metadata/state coordinators |
| `/device/capacity` | Verified capability gating for capture, defence, PTZ, and supported axes | Bounded read retry; explicit unsupported and partial connection failures are device-scoped | Metadata coordinator |
| `/alarm/list` | Account-level alarm reconciliation using a startup recovery window and incremental overlap | Bounded read retry | Alarm coordinator/reconciler |
| `/device/capture` | Request a fresh still-image URL | No automatic write retry | Scheduled capture worker |
| `/device/defence/set` | Set arming/defence state | No automatic retry | Defence switch |
| `/device/alarm/sound/set` | Set short alert, long alert, or silent mode | No automatic retry | Alarm sound select |
| `/api/v3/device/switchStatus/get` | Read exact alarm-light protocol 301 or 305 with matching echoed `data.type` and boolean `data.enable` | Bounded read retry; unsupported statuses remain device-scoped | Metadata coordinator |
| `/api/v3/device/switchStatus/set` | Write only a previously verified light protocol | No automatic write retry | Alarm flash switch |
| `/device/ptz/start` | Start bounded directional movement | No automatic retry | PTZ button |
| `/device/ptz/stop` | Stop the same movement direction | No automatic retry; always attempted during cleanup | PTZ button |

Temporary image URLs returned by capture/alarm APIs are downloaded only over
HTTPS. A dedicated connector performs connection-time DNS validation and
rejects the complete answer if any address is private, loopback, link-local,
multicast, unspecified, reserved, or otherwise non-global. The connector then
uses those exact accepted results, eliminating a separate preflight lookup and
DNS-rebinding time gap. Redirects are not followed, and the response must have
an `image/*` content type and fit within the 8 MiB hard limit.

## Official references

The official documentation site currently points readers toward its newer
documentation center, while some detailed public field references remain in
the legacy OpenSDK pages. These links are retained as public vendor references,
not as permission to use private endpoints:

- China platform documentation overview:
  `https://open.ys7.com/doc/zh/`
- Public data structures for device/channel/alarm lists:
  `https://open.ys7.com/doc/zh/pc/group__data.html`
- Public device-operation/PTZ semantics:
  `https://open.ys7.com/doc/zh/pc/group__device.html`
- Current Open Platform API documentation: `https://open.ys7.com/help/19`
- Alarm list reference: `https://open.ys7.com/help/986`
- Open Platform Message Push feature development reference:
  `https://open.ys7.com/help/5130`
- Open Platform Console > Cloud Signaling > Message Push:
  `https://open.ys7.com/console/messagePush.html`
- Public platform programming guide and CN host distinction:
  `https://open.ys7.com/doc/zh/pc/page2.html`

Before changing an endpoint or field mapping, verify it against the current
official China Open Platform documentation and update the parser tests with
synthetic, redacted fixtures.

## Third-party corroboration, not specification

The following CN projects were reviewed as evidence that public Open Platform
paths are used in deployed software. They are not protocol authorities and do
not justify a new endpoint, response field, or Home Assistant entity without a
matching official EZVIZ reference:

- `dscao/ezviz` at commit
  `08905b0ed8a91a74e59c96355d154ad1f6ea1a31` corroborates the public CN LAPP
  token, device/channel list, capacity, capture, defence, alarm sound, and PTZ
  paths and the capability names used here. Its GPL source is not copied. Its
  blocking requests, response logging, private endpoints, and other assumptions
  are not adopted.
- `ha-china/ezviz_push` at commit
  `e60e27d5f2c46bd932685ad5f20ba0734ebeec61` corroborates declaring Home
  Assistant's `webhook` dependency and acknowledging `messageId` before heavy
  processing. Its permissive decoding and payload, serial, and URL logging are
  not adopted.
- `liuzexier/ezviz_cn` at commit
  `adbda754965bd5eeba0f5b77de35674318089943` primarily uses private mobile-app
  `/v3/...` APIs and bundled reverse-engineered client behavior. That
  architecture is outside this integration's approved scope.

Other public CN examples were used only to cross-check the spelling of
`/device/alarm/sound/set`, `/alarm/list` time/status fields, and PTZ stop
direction values. Official EZVIZ documentation and strict synthetic parser
tests remain the acceptance authority.

## Authentication and token behavior

- AppKey/AppSecret are sent only to `/token/get`.
- Access tokens remain in memory and are never entity attributes or diagnostic
  output.
- Concurrent callers share one token-refresh lock.
- A documented token-rejection response invalidates the cached token and
  permits one replay with a fresh token.
- Repeated authentication failure becomes a Home Assistant reauthentication
  request rather than an infinite retry loop.

## Pagination and batching

- Device and channel lists are fetched page by page with a hard 1000-page
  guard. Their shared pagination helper accepts page sizes from 1 to 1000.
- Credential validation uses the normal 50-device page size while returning the
  complete accessible device snapshot for the setup selector.
- When **Include shared (managed) devices** is enabled, `camera/list` entries
  carrying the official recipient value `isShared=2` are grouped by device
  serial and added as minimal shared-device snapshots. The official values are
  `0` for not shared, `1` for the sharing owner, and `2` for the recipient; this
  field is therefore not parsed as a generic boolean. Direct `/device/list`
  metadata wins when both endpoints expose the same serial. Shared devices do
  not receive guessed firmware, defence, or alarm-sound state; controls remain
  gated by readable state and `/device/capacity`.
- `/alarm/list` additionally enforces the endpoint's documented maximum page
  size of 50 with `alarmType=-1` and `status=2`. The first poll after setup
  recovers the latest 24 hours. Each successful poll advances an in-memory
  checkpoint, and later polls start at that checkpoint with a 60-second overlap.
  Empty successful polls advance it; failures do not. Every range remains
  bounded by the current 24-hour lookback and the official seven-day maximum.
- State and alarms use account-level list calls, then apply one shared local
  include/exclude filter. Entries created before explicit filter modes existed
  retain their legacy include-list meaning when `selected_devices` is present.
- Exclude mode with an empty list includes newly discovered devices by default;
  include mode requires an explicit selection.
- Capabilities remain per-device calls and run only on the slow metadata
  schedule. An explicit unsupported response omits capability-backed entities
  for that device. A connection failure is isolated only when another device
  succeeds; authentication, rate-limit, protocol, total-connection, and unknown
  application failures keep the metadata refresh failed.
- Alarm-sound state also runs only on the slow metadata schedule. Directly
  controlled devices use `/device/status/get` with `channel=1`; only values
  `0`, `1`, and `2` together with `support_alarm_voice > 0` create the writable
  select. Missing fields, `-1`, missing/zero capability, and
  explicit unsupported responses do not create it. Shared-recipient devices
  are not queried for this control, and transient connection failures retain
  any valid list value until a later metadata refresh.
- Alarm-light capability is independent: only `support_active_defense=2` is a
  candidate. On metadata refresh, direct online devices are probed with 301
  and 305 (previously verified protocol first). An exact echoed `data.type`
  and boolean state are mandatory. Successful protocol evidence is persisted
  under a non-reversible device UUID; an offline or transiently unreachable
  device retains its entity but exposes no state until a new verified read.
  An unsupported public status leaves the switch absent and contributes only
  an aggregate diagnostic reason. No private mobile API is used.
  Reading state cannot prove that an application holds write permission. A
  rejected setter remains an error; state is never changed optimistically.

## Event reconciliation

Polling and the optional webhook normalize into immutable alarm/calling event
models. The reconciler:

- prefers the official alarm ID for deduplication;
- uses a deterministic hash when the ID is absent;
- rejects delayed events older than the current channel state;
- keeps at most 512 recent hashes for at most 24 hours;
- persists hashes/timestamps only;
- records verified semantic evidence only after accepting a real event;
- drives the same motion, person, last-type, last-time, and latest-alarm-image
  entities regardless of transport;
- maps `ys.calling` `status` values 1/2/3 to ring/answer/hang-up state;
  `action` is delivery processing and is not used as lifecycle state;
- keeps each calling lifecycle stage distinct in deduplication and keeps
  calling images separate from alarm and scheduled-capture images.

The capability parser preserves documented raw `support_alarm_voice`,
`support_active_defense`, and `support_pir_detect` levels alongside capture,
defence, and PTZ fields. An unknown positive voice level remains a level, not
an invented fourth sound mode.

Motion/person entities are based on accepted event types from the official
[alarm type reference](https://open.ys7.com/help/981), not device models or
undocumented capability names. The shared classifier accepts both the numeric
`alarmType` returned by alarm-list polling and the corresponding textual type
used by Cloud Push. It covers basic motion/PIR, intelligent detection
(`15010`/`intelligentDetection`), human/face analytics, line and region rules,
entry/exit/intrusion, vehicle, and pet detection. Explicit human and face types
establish both person and motion semantics; generic smart, vehicle, pet, and
rule-trigger types establish motion only. Unrelated alarms and unknown values
remain generic alarms and do not create motion or person entities.

## Cloud Push callback

The callback is the preferred near-real-time event path. Alarm-list polling is
reconciliation and recovery for missed or delayed events, Home Assistant
restarts, and deployments whose webhook delivery has not yet been verified.

The callback consumes the Open Platform Message Push envelope with `header`
and `body` objects. Non-empty string `messageId`, `deviceId`, and `type` fields plus
numeric millisecond `messageTime` are required to form a provider
acknowledgement. Optional `header.channelNo` and documented `body.channel` are
accepted only as integers and must agree when both are present. The request
thread performs only a 1 MiB/1.5-second bounded body read, strict JSON/envelope
validation, and a non-blocking queue operation, then returns HTTP 200 with the
exact original `messageId`.

Message-specific parsing, target validation, deduplication, entity discovery,
and store scheduling happen in the local worker after acknowledgement.
`ys.alarm` requires the documented ISO `alarmTime`, `alarmType`, and channel.
`ys.calling` uses `status` 1/2/3 and numeric millisecond `callingTime`, with the
documented ISO `timestamp` as fallback. Because the CN ISO examples omit an
offset, offset-less values are interpreted as `Asia/Shanghai` before being
normalized to UTC. Supported types are `ys.alarm` and `ys.calling`. Valid but unsupported,
unrelated, or locally dropped messages remain acknowledged and are represented
only by aggregate diagnostics counters. Raw payloads and message identifiers
are never logged or included in diagnostics.

The worker performs target membership checks against the filtered metadata
snapshot. This check cannot influence the already-created HTTP response, so
excluded devices, newly added devices not yet discovered, unsupported message
types, and invalid message-specific bodies cannot cause provider retry storms.

The official protocol also defines an optional HMAC-SHA1 signature using the
callback signing secret. This release intentionally does not collect that
secret or verify the signature. The webhook URL remains a sensitive bearer
credential. Deployments requiring cryptographically authenticated callback
origin must enforce authentication before Home Assistant or leave the webhook
disabled.

## Explicit exclusions

Private mobile-app APIs, reverse-engineered account login, QR/session-cookie
flows, browser scraping, undocumented notification subscription, and vendor app
impersonation are not used. Live video, RTSP, playback, recording, and camera
entities are also outside this integration.

Unknown response fields are not promoted to Home Assistant behavior without an
official reference, typed parser support, and tests.

The hub exposes only local request-attempt and rate-limit counters since this
integration runtime started. No public endpoint for an application-specific
remaining API quota has been verified, so the counters are never labelled as
assigned quota or remaining balance. A local refresh button updates display
without a vendor request. Device refresh buttons run one account-batched
state/alarm/metadata cycle per hub at most once a minute.
