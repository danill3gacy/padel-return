# Winning back lapsed padel-club members

[![tests](https://github.com/danill3gacy/padel-return/actions/workflows/ci.yml/badge.svg)](https://github.com/danill3gacy/padel-return/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![dependencies: none](https://img.shields.io/badge/dependencies-none-success)](requirements.txt)

The product takes an export of a club's member base, finds the people who have stopped coming,
works out the likely reason from their booking history, and wins them back with a personal offer —
**not a discount, but a specific game**: a slot at their usual time with partners at the right
level. The club pays a percentage of the revenue from the members who return.

It runs with **zero dependencies**: only the Python 3.11+ standard library and SQLite. An LLM is
optional — with no API key the product works on rules and templates.

## Headline numbers

| Measure | Value |
|---|---|
| Return rate, campaign group | **7.8%** (31 of 400) |
| Control group | **2.0%** (1 of 50) |
| Honest uplift over control | **+5.8 p.p.** |
| Incremental revenue for the run | **₽263,200** |
| Club's 25% commission | **₽65,800** |
| Scale of the run | 1,200 members, 15,000 bookings |
| Codebase | ~3,900 lines of Python, 21 modules, **zero external dependencies** |
| Tests | **18**, CI on Python 3.11 and 3.12 |

> These figures come from one full run on a generated club base
> (`bash demo.sh`, reproducible in a minute). It is a demonstration of the mechanics and the
> economics of the model, not a deployment report.

## The problem

A club's member base drains away silently. The reports carry revenue and court utilisation, but
there is no line called "stopped coming": a lapsed member simply is not in the statistics. Calling
around by hand does not scale, and a blanket discount mailshot hits everyone identically and
devalues the membership. And the real objection is usually not price — it is "there is no one to
play with, and rounding up a four is a hassle".

## The solution

Import the export → find the dormant members → hypothesise the reason → build a personal offer →
run the campaign over messengers → parse the replies → report back to the club with an honest
uplift figure. The club pays a percentage of the revenue from those who return.

| Stage | Module | What it does |
|---|---|---|
| Import | `importer.py` | CSV from any CRM: column auto-detection, saved mapping |
| Features | `features.py` | A partnership graph reconstructed from shared court and time slot |
| Segmentation | `segmentation.py` | "Dormant" = a broken personal rhythm, not a fixed 60 days |
| Reason | `reasons.py` | A hypothesis for the departure, drawn from a closed list |
| Offer | `offers.py` | Assembling fours, "two players already, we need a third" |
| Campaign | `campaign.py` | Three touches, quiet hours, a confirmation queue |
| Replies | `inbox.py` | Classification, conversation, escalation to the front desk |
| Money | `attribution.py` | Returns, revenue, uplift over control |

## Engineering decisions

- **Personal rhythm instead of a fixed threshold.** `days_since_last > max(45, avg_interval * 3)`:
  a weekly player who has been gone a month has left; a monthly one has not. A fixed threshold
  blends the two and wrecks conversion.
- **An offer, not a discount.** The hierarchy is strict: a game already assembled → a game coming
  together → their usual slot → a tournament → a beginners' session → and only at the very end,
  price.
- **The offer grows stronger as the campaign runs.** Those who accept accumulate in a pool, so the
  next recipient no longer hears "a court is free" but "two players at your level already".
- **Human in the loop.** The agent runs the conversation but never writes the booking — it files a
  task for the front desk with a "Confirm" button.
- **A control group as the basis of every calculation.** The club pays for the lift over "they would
  have come back anyway", not for every return indiscriminately.

## Stack

The Python 3.11+ standard library and SQLite. Channels: WhatsApp (Wazzup / Radist / i-Digital),
SMS, Telegram and console, all behind one interface. The admin surface is a Telegram bot; there is
deliberately no web UI. The LLM is optional: with no key, rules and templates take over.

---

## Run it right now

```bash
bash demo.sh
```

The script generates a plausible club base of 1,200 members and 15,000 bookings, runs the full
cycle, and drops a `report.html` next to it — the very report that gets handed to the club.

A typical run prints (output is in Russian; English gloss on the right):

```
Основная группа : 31/400 = 7.8%   выручка 308 000 ₽    # campaign group; revenue ₽308,000
Контроль        : 1/50  = 2.0%                         # control group
Прирост         : +5.8 п.п.                            # uplift, percentage points
Инкрементальная выручка: 263 200 ₽                     # incremental revenue
К оплате (25%)  : 65 800 ₽                             # club's fee at 25%
```

Tests:

```bash
python3 -m unittest discover -s tests -v
```

## The full cycle by hand

`--club` takes the club's name; `"Мой клуб"` below is simply a placeholder meaning "my club".

```bash
python3 tools/gen_sample_data.py --clients 1200 --out data/sample     # or your own export

python3 -m padelreturn.cli --club "Мой клуб" init --courts 4
python3 -m padelreturn.cli --club "Мой клуб" import \
        --clients clients.csv --bookings bookings.csv
python3 -m padelreturn.cli --club "Мой клуб" segment --name "Возврат, август"
python3 -m padelreturn.cli --club "Мой клуб" plan --campaign 1 --limit 400
python3 -m padelreturn.cli --club "Мой клуб" preview --campaign 1 -n 30   # proofread by eye
python3 -m padelreturn.cli --club "Мой клуб" approve --campaign 1
python3 -m padelreturn.cli --club "Мой клуб" run --campaign 1

# after 5 and 12 days
python3 -m padelreturn.cli --club "Мой клуб" followups --campaign 1

# after 21 and 60 days — with a fresh bookings export
python3 -m padelreturn.cli --club "Мой клуб" attribute --campaign 1 --bookings bookings_new.csv
python3 -m padelreturn.cli --club "Мой клуб" report --campaign 1 --out report.html
```

---

## Configuration

Everything goes through environment variables; nothing is hardcoded.

```bash
# LLM (optional — without it, rules and templates are used)
export PADEL_LLM_PROVIDER=anthropic          # none | anthropic | openai
export ANTHROPIC_API_KEY=sk-...
export PADEL_LLM_MODEL=claude-sonnet-4-5

# WhatsApp through a Russian provider (Wazzup, Radist, i-Digital)
export PADEL_CHANNEL=whatsapp
export PADEL_WA_URL=https://api.wazzup24.com/v3/message
export PADEL_WA_KEY=...
export PADEL_WA_CHANNEL=...

# Telegram: the admin bot, and a free channel for anyone already subscribed
export PADEL_TG_TOKEN=...

# A run with no real sends, but with cost still counted
export PADEL_DRY_RUN=1
```

Segmentation parameters, cadence and attribution windows live in `padelreturn/config.py`.
Per-club settings (courts, opening hours, prices, peak times) live in `clubs.settings_json` and are
set by the `init` command.

---

## What matters about the channels

**On Telegram you cannot message someone by phone number.** A bot can only write to people who have
pressed `/start` themselves. So the cold touch goes out over the **WhatsApp Business API with
templates** (roughly ₽5–9 per message), and inside that very first conversation the person is moved
across to the club's Telegram bot, where every later touch is free. `channels.pick()` selects
Telegram automatically whenever a contact has a `tg_chat_id`.

Building up the base inside Telegram is the product's main long-term metric: it drives the cost per
touch in later campaigns to zero.

---

## Legal framework

Not legal advice, but it is the first thing any sensible owner will ask about. In Russia:

- **Federal Law 152-FZ (personal data)**: the club is the data controller and you are a processor
  acting on its instructions. That instruction has to be in the contract, and hosting has to be in
  Russia.
- **Federal Law 38-FZ, art. 18 (advertising)**: consent to marketing messages must be part of the
  club's terms, accepted by the member at their first booking. Verify it before a campaign starts.
- **The sender is the club, not you.** The WhatsApp account and the Telegram bot are registered to
  the club.
- An opt-out in every first message, honoured instantly, with a stop list shared across all
  campaigns.

The product supports all of this: a `consent` field, a `stop_list`, an opt-out footer on the first
touch, exclusion of anyone contacted recently, and idempotent sending.

---

## Milestones (from the PRD)

- **M0** — a campaign run by hand: `--channel console`, messages sent manually from the club's phone. ✅
- **M1** — automatic sending, cadence, confirmation queue. ✅
- **M2** — conversational agent, escalations. ✅
- **M3** — offer engine, assembling fours, migrating the base to Telegram. ✅ (basic version)

Deliberately **not** built until the fifth club: API integrations with CRMs, a web admin panel,
multi-language support, in-product billing, and any analytics beyond a single report.

---

## License

Internal development. The code is written for a single developer and is deliberately boring:
standard library, SQLite, no frameworks.

<sub>The product's messages, reports and code are in Russian — it is built for Russian-market
clubs.</sub>
