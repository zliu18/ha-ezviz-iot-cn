# Troubleshooting

Start with **Settings > System > Logs** and the config-entry diagnostics. Do not
paste credentials, serial numbers, webhook URLs, alarm payloads, or images into
an issue.

## Integration cannot be added

### Invalid authentication

- Confirm the values are a China Open Platform `AppKey` and `AppSecret`, not an
  EZVIZ Cloud Video app username/password.
- Remove leading/trailing whitespace from the AppKey.
- Confirm the application is enabled and still authorized in the platform.
- Use **Reconfigure** or the Home Assistant reauthentication prompt to rotate a
  secret. Reusing the same AppKey preserves device and entity identity.

### Cannot connect

- Verify Home Assistant can resolve and reach `open.ys7.com` over HTTPS.
- Check DNS, outbound firewall, proxy, and system time.
- The integration does not require inbound ports unless Native webhook is
  enabled.

## No devices or missing entities

- Confirm the Open Platform application can access the expected devices. App
  sharing in the consumer app does not necessarily grant an Open Platform
  application the same scope.
- Reopen integration options and check **Device filter mode** and **Devices**.
  Include mode keeps only the selected devices; exclude mode ignores the
  selected devices. The default exclude mode with no selection includes all
  current and future devices.
- Writable entity creation is capability/state-driven. Unsupported PTZ,
  defence, alarm sound, or capture entities are intentionally absent.
- Alarm sound requires both `support_alarm_voice > 0` and a valid `0/1/2`
  `alarmSoundMode` from device status. A readable mode by itself is not enough.
- Alarm flash is independent of sound. It requires `support_active_defense=2`
  and a matching 301/305 response from the public switch-status endpoint.
  A light-capable device with unsupported public status has no writable switch;
  diagnostics include only an aggregate reason. Do not use private app APIs.
- Motion, person, alarm, doorbell, calling, and event-image entities are absent
  until that channel produces the corresponding real event semantic. This is
  deliberate capability evidence, not a discovery failure.
- Once observed, event entity support survives restart. Its live value remains
  inactive or unavailable until a new event arrives; the integration does not
  invent a placeholder event.
- Metadata refresh defaults to 6 hours. Reload the integration after platform
  permission or device-capability changes.
- A temporarily missing device remains registered but becomes unavailable; it
  is not immediately deleted from the registry.
- Excluding a device also leaves its existing registry entities in place but
  unavailable. Re-including it restores the same stable entity identities.

## Motion/person does not update

- Cloud Push is the preferred near-real-time event path. Confirm that the
  webhook is enabled and receiving real device events.
- Keep **Alarm polling** enabled until Cloud Push is verified. Polling recovers
  missed or delayed events and coordinates event state after a restart.
- Confirm the camera's alarm/defence configuration allows the EZVIZ platform to
  create alarm records.
- Motion/person entities are alarm-event projections, not algorithm controls.
  Official motion, intelligent-detection, body/person, face, region-rule,
  vehicle, and pet events activate motion as appropriate. Explicit person and
  face events also activate person. Unknown and unrelated alarm types update
  last-alarm diagnostics without fabricating motion or person state.
- Increase the alarm interval if API quota errors occur; a throttled poller
  cannot provide low latency.
- The motion/person state resets after the configured hold time. A newer event
  restarts the timer.

## API quota or rate limiting

The integration cannot discover your application's quota. Increase intervals
in this order:

1. Disable scheduled captures or increase the capture interval.
2. Increase alarm polling if native Push is reliable.
3. Increase device-state polling.
4. Leave metadata on a multi-hour interval.

Diagnostics show queue high-water marks and coordinator health without device
identifiers. Control actions are deliberately not retried automatically.

## Near-real-time Cloud Push Webhook

### URL shows `-`

Enable **Native webhook**, save, wait for the integration reload, then reopen
the options page. The generated URL is a bearer secret.

### Only a relative path is shown

Home Assistant does not have a usable external base URL. Publish the displayed
`/api/webhook/...` path through a trusted HTTPS reverse proxy or configure an
external URL in Home Assistant.

### Push test works but device alarms do not

- Confirm the application sends the China Cloud Push `header`/`body` envelope,
  not the legacy direct alarm object.
- A successful callback response must be HTTP 200 with the original
  `messageId` within two seconds. Repeated failures may degrade Push delivery.
- Confirm the event device/channel belongs to this AppKey. Unrelated messages
  are acknowledged to prevent retries, then ignored and counted locally.
- Device filtering is applied only after acknowledgement. Messages for an
  excluded device should increase the unknown-target aggregate without causing
  callback retries or Push-service degradation.
- For `ys.calling`, confirm lifecycle is supplied as integer `status` 1, 2, or
  3. The `action` field is delivery processing and is not ring/answer/hang-up.
- Do not reuse a webhook from another EZVIZ integration.
- Keep polling enabled until real delivery is stable for this application.
  Platform Push behavior may vary by application/account configuration.

### Security guidance

- Treat the generated webhook URL as a sensitive credential.
- Use HTTPS and expose only the exact `/api/webhook/...` path, never the Home
  Assistant management interface.
- Do not share the URL, AppKey, AppSecret, or access token in screenshots,
  logs, diagnostics, or issue reports.
- Rotate the URL by removing the integration entry and configuring it again if
  the URL is disclosed.
- Normal option changes, reauthentication, and same-AppKey reconfiguration do
  not rotate the URL.

## Images do not update

- Scheduled capture is off by default and has a minimum 5-minute interval.
- The device must advertise capture support.
- Alarm and calling images require a corresponding event with an HTTPS image
  URL. Their entities are created only after that evidence is observed.
- Image entities remain unavailable until a real cached image or downloadable
  event image exists; no placeholder image is published.
- Each image must be HTTPS, have an `image/*` content type, and be no larger
  than 8 MiB.
- The image hostname must resolve only to public IP addresses and redirects are
  not followed, so private-network or redirecting URLs are rejected.
- Encrypted alarm-image decryption is not implemented. Such images may be
  unavailable even when the alarm event itself works.
- Scheduled capture, alarm, and calling images use separate memory-only cache
  slots and are cleared on reload/restart.

## PTZ or controls fail

- Controls appear only when verified capability and readable state requirements
  are satisfied.
- **Refresh status** is available on each device, but the buttons share one
  hub-wide one-minute cooldown. It refreshes account-level state and alarms
  plus capability, alarm-sound, and alarm-light metadata. It is not a switch
  for internal motion-detection configuration.
- Hub API counts are local to the current integration run, not the assigned
  quota or remaining balance. Their display updates at most once a minute;
  the Hub refresh-counts button reads locally without another API call.
- PTZ direction buttons move for a bounded 0.5 seconds and then send the
  matching stop automatically. There is no standalone Stop button because the
  stop endpoint requires the active direction and a completed bounded press has
  no remaining movement to stop.
- A failed write keeps the last known state until the next successful refresh.
- Check that another app or automation is not issuing competing PTZ commands.

## Reporting a problem

Include:

- integration version and Home Assistant version;
- which entity class or option is affected;
- sanitized error code/message;
- redacted diagnostics;
- whether polling, native webhook, or both are enabled.

Never include AppKey, AppSecret, access token, serial number, webhook URL,
temporary image URL, raw response/payload, or image bytes.
