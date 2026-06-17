# Mi Fitness — Home Assistant

Unofficial Home Assistant integration that pulls health & fitness data (steps,
heart rate, sleep, vitality, SpO₂, weight, stand hours …) from the Xiaomi **Mi
Fitness / MiHealth** cloud. Data is read at the account level — whatever your
Xiaomi devices (bands, scales, …) sync to Mi Fitness shows up, regardless of
which device produced it.

> ⚠️ **Disclaimer — educational / unofficial.**
> Not affiliated with, authorized, or endorsed by Xiaomi. It talks to a private
> cloud API by reusing your own account session, which may break at any time and
> may be against Xiaomi's Terms of Service. Use at your own risk, with your own
> account only.

---

## Features

- Sensors: steps, distance, calories, heart rate (avg/max/min/resting), sleep
  (duration, stages, quality, chronotype), vitality, stand hours, SpO₂, weight.
- Silent token refresh using `passToken` — survives `serviceToken` expiry
  without re-doing 2FA.
- Custom Lovelace cards for activity and sleep (in [`www/`](www/)).

---

## Installation (HACS)

1. HACS → **⋮** → **Custom repositories**.
2. Add `https://github.com/batareya16/ha-mi-fitness` as category **Integration**.
3. Install **Mi Fitness**, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → Mi Fitness**.

### Manual install

Copy `custom_components/mi_fitness/` into your HA `config/custom_components/`
and restart.

---

## Setup

Two authentication paths in the config flow:

### A. Username + password (recommended)
Enter your Xiaomi account email/phone and password. If 2FA is enabled, a code
is emailed to you and entered in the next step. The integration stores a
`passToken` so it can refresh expired session tokens silently afterwards.

### B. Manual token entry
Advanced fallback for accounts where automated login fails. Supply credentials
obtained from your own Mi Fitness account session:

- `ssecurity`, `userId`, `cUserId` — from the `serviceLoginAuth2` response
- `passToken` — from the `serviceLoginAuth2` `Set-Cookie` header
- `serviceToken` — from the STS `Set-Cookie` header

Paste them into the form. `passToken` is optional but strongly recommended (it
enables silent refresh).

---

## Lovelace cards

The integration ships custom cards (in
[`custom_components/mi_fitness/www/`](custom_components/mi_fitness/www/)) and
**registers them automatically** — no manual resource setup needed.

Just add them to a dashboard:

```yaml
type: custom:mi-fitness-activity-card
# also available:
#   custom:mi-fitness-activity-streak-card
#   custom:mi-fitness-sleep-card
#   custom:mi-fitness-sleep-streak-card
```

> After updating the integration, hard-refresh the browser (Ctrl+Shift+R) so
> the new card code loads.

---

## Privacy

No credentials or device identifiers are committed to this repository. Your
tokens are stored only in your own Home Assistant instance
(`.storage/core.config_entries`) — never commit your HA config.

## License

[MIT](LICENSE)
