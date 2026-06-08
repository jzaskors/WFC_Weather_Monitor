# WFC West Harbor Weather Monitor

Texts and emails staff when **kayak/SUP rentals** or **sailing programs** should
HALT at West Harbor (Oyster Bay), and again when they clear. Runs free on a
GitHub Actions schedule — no server to maintain.

It uses the same decision logic as the dashboard: wind, gusts, offshore-wind
direction (southerlies push paddlers out of your north-facing launch),
thunderstorms, heavy rain, low visibility, and live NWS marine/storm warnings.

---

## One-time setup (~20 min)

### 1. Make a repo
Create a **private** GitHub repo and upload these four files, keeping the folders:
```
monitor.py
requirements.txt
.github/workflows/weather-monitor.yml
README.md
```

### 2. Add your secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add each of these (skip none if you want both channels):

| Secret | What it is |
|---|---|
| `TWILIO_SID` | Twilio Account SID |
| `TWILIO_TOKEN` | Twilio Auth Token |
| `TWILIO_FROM` | Your Twilio phone number, e.g. `+15165550100` |
| `ALERT_SMS_TO` | Recipients, comma-separated, e.g. `+15165551234,+15165555678` |
| `SMTP_HOST` | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | Sending email address |
| `SMTP_PASS` | App password (see below) |
| `EMAIL_FROM` | Usually same as `SMTP_USER` |
| `EMAIL_TO` | Recipients, comma-separated |

**Twilio:** sign up, get a number (free trial works for testing; ~$1/mo + pennies
per text for production). Copy the SID/token from the console.

**Gmail:** turn on 2-factor auth, then create an **App Password**
(Google Account → Security → App passwords) and use that 16-char value as
`SMTP_PASS` — not your normal password.

### 3. Turn it on
The workflow runs automatically every ~10 minutes. To test immediately:
Actions tab → **WFC Weather Monitor** → **Run workflow**. Check the run log to
confirm it fetched data and (if conditions warranted) sent alerts.

---

## How alerts behave
- You're notified only on a **change**: when an activity flips *into* HALT, and
  once more when it clears. No repeat spam while it stays halted.
- Each activity (rentals vs sailing) is tracked separately — you'll know exactly
  which to shut.
- Alerts only fire during operating hours (default **7am–8pm ET**). If it's bad
  at opening, you get a fresh alert that morning.

## Tuning
- **Thresholds:** edit the `THRESHOLDS` block at the top of `monitor.py`. Keep
  them matched to the dashboard so the two never disagree.
- **Offshore arc:** default `150–240°` (winds from S/SW). If your real exposure
  differs, adjust `offshoreArc`.
- **Hours:** change `OPEN_HOUR` / `CLOSE_HOUR`.
- **Also alert on CAUTION:** set `ALERT_ON_CAUTION = True` (noisier).
- **Off-season:** disable the workflow (Actions → ⋯ → Disable) and re-enable in spring.

## Limits (same as the dashboard)
- No real-time lightning-strike feed. Storm risk is forecast/observation-based
  (weather code + CAPE + NWS warnings). **Always confirm lightning by eye/ear and
  apply the 30-30 rule** — this is an assist, not a replacement for a human call.
- Harbor wave height is a coarse global-model estimate; wind + direction is the
  real chop signal.
- GitHub's scheduler is best-effort and can lag a few minutes under load.
