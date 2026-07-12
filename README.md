# Fuel Intelligence Engine (FIE) + NCR Price Map

A personal fuel-price system for Metro Manila: press **Refresh** and the
engine runs a complete new investigation — discovering, collecting,
normalizing, matching, verifying, and conflict-resolving public evidence —
then the map shows the latest verified pump price for ~1,000 NCR stations
around you.

**The engine never guesses.** Every price is verified, clearly `derived`,
or honestly `Price unavailable`.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e .

.venv/bin/fie serve            # http://127.0.0.1:8000 — map + REST API
.venv/bin/fie refresh          # run an investigation from the terminal
.venv/bin/fie prices           # inspect stored verified prices
```

Open the site, allow location, and press **Refresh prices**. Drag the map
to compare stations; the sidebar lists the 30 nearest with distances.

## Where the prices come from

Per-station pump prices are not published by the oil companies, so the
engine triangulates what *is* public:

1. **GasWatch PH** (`providers/gaswatch.py`) — per-station datasets for all
   17 NCR cities, fed by the DOE's official weekly retail-price advisory
   (every Tuesday) plus community reports. Primary witness; evidence is
   judged against its weekly cadence.
2. **Official adjustment ledger** (`providers/adjustments.py` + store) —
   PH fuel companies announce exact per-liter adjustments weekly (effective
   Tuesday 6 a.m.). The engine parses these announcements from GMA News'
   RSS-indexed advisories into a persistent ledger. Parsing is
   conservative: an ambiguous sentence contributes nothing.
3. **Derivation Engine** (`derivation/engine.py`) — when direct evidence
   goes stale, `last verified baseline + announced adjustments since` is
   still hard knowledge (e.g. ₱70.00 verified Jul 5, +₱3.30 effective
   Jul 8 → ₱73.30). Served as status `derived`, capped at MEDIUM
   confidence, with the full arithmetic in `derivation_note`. Recomputed
   from the immutable baseline every refresh — never derived from a derived
   value, never beyond 14 days from verified ground truth.
4. **Official brand pages, Facebook (Graph token), OCR price-board photos,
   web search** — additional witnesses that plug in via config; the engine
   corroborates whatever is available.

The canonical station database (`fie/data/stations.json`, 964 stations) is
bootstrapped from the same public directory:
`.venv/bin/python scripts/import_gaswatch_stations.py` re-imports it.

## Architecture

```
Station Database      fie/stationdb/     canonical identities (964 NCR stations)
Discovery Engine      fie/discovery/     finds candidate sources, never prices
Provider Framework    fie/providers/     independent witnesses -> standard RawEvidence
Evidence Collector    fie/collection/    concurrent, fault-isolated retrieval
Normalizer            fie/normalization/ cleans or rejects every claim (PHP/L)
Station Matching      fie/matching/      validated-hint fast path + fuzzy resolution
Verification Engine   fie/verification/  challenges evidence, agreement clusters
Confidence Engine     fie/confidence/    cadence-aware weighted scoring
Conflict Resolution   fie/resolution/    exactly one price survives, never averaged
Verified Price Store  fie/store/         SQLite: prices, ledger, history, reliability
Derivation Engine     fie/derivation/    baseline + official adjustments overlay
REST API + Map        fie/api/, web/     thin consumers, no business logic
```

Composition root: `fie/container.py`. Refresh sequence + per-refresh cache
(destroyed after every investigation): `fie/pipeline/orchestrator.py`.

### How prices earn HIGH confidence

PH pump prices move only through officially announced weekly adjustments
(effective Tuesdays). The engine exploits this (`fie/record.py`):

- **Announcement-aware recency** — evidence stays current while the
  adjustment record shows no price change since it was published.
- **Record corroboration** — a price equal to `last verified + announced
  deltas since` is confirmed by an independent evidence chain and counts
  like an agreeing witness.
- **Challenges** — a price deviating pesos from the record's expectation,
  or >10% from its own brand's regional median, is capped at MEDIUM. The
  price is served as published, but never with top confidence on a single
  witness.

Result in practice: ~97% of prices HIGH, with the anomalies honestly
marked MEDIUM.

### Rules enforced in code

- Conflicting prices are ranked (official station > official company >
  more agreeing sources > newest > highest confidence) — never averaged.
- Ambiguous station identity ⇒ evidence rejected, not assigned.
- Older evidence never overwrites a newer verified price; failed refreshes
  keep the old value as `last_successfully_verified`.
- Derived values live in overlay columns; verified ground truth is never
  contaminated.
- One failing provider never stops the engine; provider agreement with
  final verified prices feeds future reliability scoring.

## REST API

| Endpoint | Meaning |
|---|---|
| `GET /api/v1/prices` | all stored prices (verified / derived / stale / unavailable) |
| `GET /api/v1/stations` · `/stations/{id}` · `/stations/{id}/prices` | station data |
| `POST /api/v1/refresh` | run a new investigation (`{"station_ids": [...], "developer_mode": true}`) |
| `GET /api/v1/health` | liveness |

Developer Mode (set `FIE_DEVELOPER_MODE=1`, then pass `developer_mode`)
returns the full trace: providers queried, evidence accepted/rejected,
cluster formation, confidence factors, resolution ranking, stage timings.

## Configuration

`FIE_*` environment variables (see `fie/config.py`): evidence age limits,
price plausibility bounds, confidence thresholds, derivation horizon,
`FIE_FACEBOOK_GRAPH_TOKEN`, `FIE_SERPER_API_KEY`, `FIE_ADJUSTMENT_FEED_URL`.
Sources live in `fie/data/known_sources.json` — adding one is a data
change, not a code change.
