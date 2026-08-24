# WalleCube UPS — BLE protocol reference

Complete wire-format documentation for the WalleCube W150 UPS Bluetooth LE
interface, reverse-engineered from the official Android app
(`com.wallecube.ups`) and verified against real hardware. This is the
reference behind [`wallecube_ble`](custom_components/wallecube_ble/); the
[README](README.md) covers the integration itself and how the protocol was
recovered.

All multi-byte fields are **little-endian** unless noted. No pairing or
bonding is required for any of it (`Paired: no, Bonded: no, Connected: yes`).

---

## 1. GATT map

| Service | Characteristic | Properties | Encryption | Contents |
| --- | --- | --- | --- | --- |
| `F0A1` | `F0B1` | notify | none — **cleartext** | Telemetry, ~1 Hz ([§2](#2-f0b1-telemetry-frame)) |
| `F0A1` | `F0B2` | read / write | AES-128-CBC | Power-adapter config, 5×u16 ([§4](#4-f0b-settings-blocks)) |
| `F0A1` | `F0B3` | read / write | AES-128-CBC | Buzzer mode, 1×u16 |
| `F0A1` | `F0B4` | read / write | AES-128-CBC | Sleep timeout + current threshold, 2×u16 |
| `F0A1` | `F0B5` | write | AES-128-CBC | **`doSystemReset` — never write this** |
| `F0A1` | `F0B7` | read / write | AES-128-CBC | Unidentified; changes with display language |
| `F0A1` | `F0B8` | read / write | AES-128-CBC | Display language, 1×u16 |
| `F0A1` | `F0B9` | read / write | AES-128-CBC | Display temperature unit, 1×u16 |
| `F0A2` | `F0C1` | write + notify | AES-128-CBC | Request/response command channel ([§5](#5-f0c1-command-channel)) |

Full 128-bit UUIDs follow the Bluetooth base pattern, e.g. `F0B1` is
`0000f0b1-0000-1000-8000-00805f9b34fb`.

Two structural facts matter more than any individual field:

- **The characteristic is the command selector, not an opcode inside the
  frame.** Each setting has its own characteristic, so a malformed frame
  cannot be reinterpreted as a different command. `doSystemReset` is
  reachable only through `F0B5`, which nothing else uses.
- **`F0B*` reads are plain GATT reads** — no auth code, no request frame,
  no round trip. Only writes need the auth code. Reading every setting is
  therefore completely safe.

---

## 2. `F0B1` telemetry frame

40 bytes, pushed by notification roughly once per second while connected,
in cleartext. Frames of any other length, or with a different header, are
not telemetry and are discarded.

| Offset | Size | Type | Field | Scale | Unit |
| --- | --- | --- | --- | --- | --- |
| 0 | 2 | u16 | Message type — const `0x0051` | — | — |
| 2 | 2 | u16 | Input voltage | ÷1000 | V |
| 4 | 2 | u16 | Input current | ÷1000 | A |
| 6 | 2 | u16 | Output voltage | ÷1000 | V |
| 8 | 2 | u16 | Output current | ÷1000 | A |
| 10 | 2 | u16 | **Battery percentage** | **÷10** | % |
| 12 | 2 | u16 | Battery pack voltage | ÷1000 | V |
| 14 | 2 | u16 | Cell 1 voltage | ×1 | mV |
| 16 | 2 | u16 | Cell 2 voltage | ×1 | mV |
| 18 | 2 | u16 | Cell 3 voltage | ×1 | mV |
| 20 | 2 | u16 | Cell 4 voltage | ×1 | mV |
| 22 | 2 | **s16** | Battery current (+ = charging) | ÷1000 | A |
| 24 | 2 | **s16** | Battery temperature | ÷10 | °C |
| 26 | 2 | u16 | `leftSecs` — dead on this model ([§7](#7-dead-and-unidentified-fields)) | — | — |
| 28 | 2 | u16 | Unidentified; random walk, app never reads it | — | — |
| 30 | 4 | u32 | Total energy consumption (monotonic) | ÷10⁶ | kWh |
| 34 | 4 | u32 | Unidentified; observed constant `0` | — | — |
| 38 | 2 | u16 | Status bit field ([§3](#3-status-bit-field-offset-38)) | — | — |

Pack voltage at offset 12 equals the sum of the four cell voltages.

**Two offsets are easy to get wrong, and both were:**

- **Offset 10 divides by 10, not 1000.** Every other voltage/current field
  divides by 1000, so this one reads as a constant `1000` — which is why it
  was long mistaken for a "rated capacity" constant. It is
  `batteryCapacity` in the app's `measureDecoder`, and `1000` means
  **100.0 %**. It only looked constant because the pack was full during
  every early capture. Confirmed against a real AC-loss event: with the
  adapter unplugged it tracked smoothly from 100.0 % down to 99.0 % over
  about two minutes under a 0.75 A load.
- **Offsets 22 and 24 are signed.** Read as unsigned, a below-freezing
  battery temperature reports as roughly 6553 °C.

Power is not reported by the device at all. Input, output, and battery
watts are `V × A`, which is how the app's own display derives them.

---

## 3. Status bit field (offset 38)

Bit positions come from the app's `measureDecoder` — each `and` mask traced
through the stack slot it lands in to the key it is filed under in the
result map — rather than inferred from observation.

| Mask | App key | Meaning |
| --- | --- | --- |
| `0x0001` | *(none)* | Set in every frame ever captured; app never reads it |
| `0x0004` | `overload` | Output overload |
| `0x0010` | `shutdownImminent` | Shutdown imminent |
| `0x0080` | `charging` | Charger currently active |
| `0x0100` | `discharging` | Battery current is negative |
| `0x0400` | `acOk` | Mains present |

Every word this device has produced, from a live AC-loss capture (462
frames):

| Word | Situation | Bits |
| --- | --- | --- |
| `0x0401` | On mains, idle | `acOk` |
| `0x0481` | On mains, charging | `acOk` + `charging` |
| `0x0101` | Running on battery | `discharging` |
| `0x0501` | Mains restored, not yet charging | `acOk` + `discharging` |

**Derive "on battery" from `acOk`, not from `discharging`.** That last word
is why: `discharging` only tracks the sign of the battery current, so it
stays set for a while after mains returns. A sensor keyed off that bit
reports "on battery" with the adapter plugged in.

`charging` is genuine but noisy — on a full pack it flips on nearly every
telemetry tick as the float current swings either side of zero.

The same capture also settled a question about the link itself: BLE stayed
up for the entire outage (462 frames, largest gap 1.26 s across 9 minutes),
so this UPS keeps talking happily while running on battery.

---

## 4. `F0B*` settings blocks

Each stored setting lives on its own characteristic. Reads are plain GATT
reads; the ciphertext decrypts to a `0x51` marker byte followed by `n`
little-endian u16 fields.

```
plaintext:  0x51 | u16 | u16 | ...   (zero-padded to a 16-byte multiple)
```

Note there is **no length byte** after the marker — do not parse these with
the `F0C1` layout ([§5](#5-f0c1-command-channel)), which does have one.

| Characteristic | Fields | Contents |
| --- | --- | --- |
| `F0B4` | 2 | Sleep timeout (s), sleep current threshold (mA) |
| `F0B3` | 1 | Buzzer mode — `0` mute, `1` beep once, `2` repeat |
| `F0B8` | 1 | Display language — `0` English, `1` 中文 |
| `F0B9` | 1 | Display temperature unit — `0` °C, `1` °F |
| `F0B2` | 5 | Power-adapter config (below) |

Every mapping was confirmed by round-trip: change it in the official app,
read it back, revert it, read it back again.

### 4.1 `F0B2` power-adapter block

Field names are the app's own, from `upsSettingsDecoder`. Fields 1–2 are
milliamps; 3–5 are millivolts.

| # | App field | Example | Meaning | App-editable |
| --- | --- | --- | --- | --- |
| 1 | `adapterCurrent` | 3.0 A | The adapter's rated current | yes |
| 2 | `enChargeLimit` | 2.0 A | Charge current limit | derived |
| 3 | `workVol` | 12.0 V | Working / output voltage | yes |
| 4 | `stopChargeLim` | 11.58 V | Input sag at which charging backs off | derived |
| 5 | `pwrokDectLim` | 11.5 V | Input below this = power lost | yes |

`doUpsSettings` takes three arguments for five wire fields because the app
derives the rest, and **the device requires them to agree**:

```
enChargeLimit = adapterCurrent - 1000          (1 A reserved for the load)
stopChargeLim = workVol × 0.965
pwrokDectLim  = workVol × 0.958, rounded to the nearest 0.1 V
```

These reproduce this unit's factory block exactly from just
`(3000 mA, 12000 mV)` → `(3000, 2000, 12000, 11580, 11500)`.

This is not cosmetic. Writing a new working voltage alongside the
*previously read* thresholds produces a block the device cannot reconcile,
and it resolves the conflict by **dropping the working voltage to match the
stale thresholds**. A 12 V write read back as 12 V, then came up as 9 V
after a reset — with the UPS genuinely outputting 9.02 V.

Changes to this block do not take effect until the device's reset button is
pressed.

### 4.2 Settings write frame

```
0x51 | 0x00 | authCode(4, LE) | FF FF FF FF | u16 per value
```

zero-padded to a 16-byte multiple, then AES-encrypted and written to the
characteristic for that setting. The payload begins at **offset 10**; the
`0xFFFFFFFF` is a fixed part of the header, not a placeholder. Because of
the zero padding, a single-byte value and a u16 are equivalent on the wire,
so everything is written as u16.

Writes need the auth code; reads do not.

### 4.3 Getting the payload offset wrong does not fail safe

Worth knowing before extending this. An early attempt placed the payload at
offset 6 instead of 10. The device did not reject those frames — it read
whatever bytes landed in the payload and **clamped them into its own valid
ranges**, so the write "succeeded" at the GATT layer while quietly storing
plausible-but-wrong values (the adapter block came back as
`10000, 9000, 11500, 10000, 9000`).

Hence two rules:

- **Verify every write by reading the value back**, and treat a mismatch as
  a failure.
- **Read-back proves the value reached RAM, nothing more.** It does not
  prove the frame was well-formed or that the setting survives a reset. For
  anything the device persists, the real test is: write, reset, read again.

The clamping had one upside — writing `0x0000` and `0xFFFF` and observing
where the device settled revealed its own limits:

| Field | Min | Max |
| --- | --- | --- |
| Sleep timeout | 20 s | 7200 s |
| Sleep current threshold | 20 mA | 3000 mA |
| LCD off-time | 10 s | 36000 s |
| `adapterCurrent` | — | 10000 mA |
| `enChargeLimit` | — | 9000 mA |
| `stopChargeLim` | 10000 mV | — |
| `pwrokDectLim` | 9000 mV | — |

A static min/max cannot express the W150's 150 W ceiling, so the
voltage × current *pair* must be checked before writing.

---

## 5. `F0C1` command channel

A request/response channel on service `F0A2`: write an encrypted frame,
receive the reply as a notification on the same characteristic.

```
type(1) | payload_len(1) | nonce(2) | authCode(4, LE) | payload
```

zero-padded to a 16-byte multiple. A **query** is that frame with
`payload_len = 0`.

- `nonce` is a fixed `0x7724` in the app's own code. The device echoes back
  a different value and does not appear to validate what it receives.
- `authCode` is a per-device secret ([§6](#6-crypto)). A response whose
  bytes 4–8 don't match your own auth code isn't yours — which is also how
  garbage from a wrong key is rejected.

### 5.1 Opcodes

| Type | Name | Direction | Payload |
| --- | --- | --- | --- |
| `0x41` | `getWifiStatus` | query | — → §5.2 |
| `0x42` | `getWifiScan` | query | — → scan results |
| `0x48` | `getWolMacList` | query | — → Wake-on-LAN MAC list |
| `0x4B` | `setLcdOffTime` | write | `u32` seconds |
| `0x4C` | `getLcdOffTime` | query | — → `u32` seconds |

**The `0xA2` opcode that earlier writeups of this protocol describe does not
exist.** blutter renders Dart integer constants Smi-tagged, i.e. the raw
value in the disassembly is `real_value << 1`. So `r16 = 162` is opcode
`0x51`, not `0xA2`. Verified against the one frame already known to be
correct: `getLcdOffTimeEncoder` shows `r16 = 152` → `0x4C` = 76 ✓. Every
blind probe against `0xA2` failed because the device has no concept of it.

The same tagging error is what hid the fact that settings are
one-per-characteristic: the six "`0x51`" encoders aren't six opcodes, they
target six different characteristics, and `0x51` is a channel marker — the
same value as the cleartext telemetry header.

### 5.2 Wi-Fi status payload (`0x41`)

This UPS has a Wi-Fi radio, used by the official app's cloud path.

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | Connected (bool) |
| 1 | 1 | RSSI (**s8**), dBm |
| 2 | 4 | IP address |
| 6 | 4 | Gateway |
| 10 | 4 | Netmask |
| 14 | 1 | SSID length, in **bytes** |
| 15 | *n* | SSID (UTF-8) |

Validate the SSID by byte length, not character count — a multi-byte UTF-8
SSID decodes to fewer characters than it occupies bytes.

---

## 6. Crypto

AES-128-CBC, **zero IV**, zero-padded to a 16-byte multiple. The same key
covers both `F0B*` and `F0C1`. The zero IV is fixed by the protocol and is
not a secret.

The key derivation, from `DeviceCoder`'s constructor:

```
key = raw_bytes( hex( MD5( fixed_app_salt + per_device_value ) ) )
```

The MD5 hex digest is used as an ASCII string and reinterpreted as 16 raw
bytes of AES key. The key and the auth code are **per-device** — not
universal across WalleCube units — and are deliberately not committed
anywhere in this repository.

In practice none of that matters, because the release build of the app
still carries a leftover `print("aesSecret = $aesSecret")` debug statement
(along with `authCode` and `password`) that reaches Android's logcat. See
[the README](README.md#optional-unlock-the-settings-channel) for the
three-command extraction.

---

## 7. Dead and unidentified fields

- **Runtime remaining does not exist on this model.** `leftSecs` is offset
  26, and a live AC-loss capture (462 frames, 357 of them with the mains
  genuinely disconnected) had it reading a constant `1` in *every single
  frame*. Offset 28 is not a hidden countdown either — it jitters up and
  down (188 of 356 steps downward, i.e. a random walk) and the app never
  reads it. This is settled, not outstanding.
- **Offset 34–37** (u32) has been constant `0` in every capture.
- **Status bit `0x0001`** is set in every frame and the app never reads it.
- **`F0B7` vs `F0B8`** — both flip together when the display language
  changes, so one may mirror the other or be a config-dirty flag. The
  integration writes `F0B8`.
- **Outlet control does not exist.** This model has no individually
  switchable outlets, so there is nothing to control — only to monitor.

---

## 8. Notes for anyone extending this

**Passive BLE capture on Android is a dead end.** The `btsnooz` blob in
`dumpsys bluetooth_manager` truncates every ACL packet to 10 bytes, leaving
only 3 bytes of ATT value, and recent Android builds omit the full
`btsnoop_hci.log` from `adb bugreport` entirely. Hooking
`BluetoothGatt`/`BluetoothGattCharacteristic` with Frida — or simply reading
the characteristics with your own BLE client — is both safer and strictly
more informative. A rooted phone may capture the full HCI log, but I don't
have one at the moment.

**Only one client at a time.** The device accepts a single persistent GATT
connection, so the official app cannot be connected while Home Assistant
is.

**Its BLE stack is fragile.** Repeated rapid reconnects can leave it
unresponsive to *all* clients, including the official app. Pressing the
reset button has reliably recovered it; unplugging AC will not, since the
battery keeps it running.

**USB corroborates everything for free.** Connected over USB, this UPS
implements the standard USB HID Power Device class in cleartext — macOS's
`IOPSCopyPowerSourcesInfo`, or `usbhid-ups`/NUT on Linux, report `Current
Capacity` and `Time to Empty` directly. Not usable for a wireless
integration, but an excellent way to check a BLE reading against an
independent source.
