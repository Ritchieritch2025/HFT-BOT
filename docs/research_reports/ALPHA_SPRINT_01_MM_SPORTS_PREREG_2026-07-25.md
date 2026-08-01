# ALPHA-SPRINT-01 MM-SPORTS — Sports Passive Quoting Preregistration

Date: 2026-07-25
Stratum: Sports only; primary results reported PER SPORT (subcategory);
pooled-only significance = ARTIFACT (2026-07-25 audit principle).
Independent MC budget; no reads of MM-POL or H6/H6b VALIDATE data.
Lineage: engine = MM-POL A3 (tape fills, passive both legs); pool rule
REDEFINED for the dense-flow regime — this is a new experiment, not a
politics parameter transplant (audit principle #2).

## Why sports after the politics kill

Politics maker died of 6:1 toxic:benign fills in sleepy wide books.
The tollbooth needs TRAFFIC: dense uninformed crossings and books that
reprice fast enough to dodge stale-quote sniping. In-house evidence:
S1 sports maker pilot GO (EXPT-BO2026); MLB mid edge +3.3pp over 4,171
games; sports is the only category with full-depth books for later
queue modeling.

## Design (frozen before VALIDATE read)

- Window: TRAIN 2026-07-10..16, VALIDATE 2026-07-17..22.
- Universe: category=Sports L1 + trade tape; cluster = event_ticker
  (one game/match = one independent bet); stratum = subcategory.
- Pool (TRAIN fit, sealed): per market, TRAIN median spread >= 2c AND
  TRAIN trades >= 50/day-average AND >= 200 quote rows. Rationale
  recorded: dense-flow regime, opposite of the politics pool.
- Lattice: side in {yes,no} x d in {1,2} ticks inside touch x exit
  e in {2,4}c; clips {1,5}; disaster stop -10c; passive both legs;
  maker fee both legs (frozen fallback schedule, FEE_MODELED);
  1c adverse haircut on any taker liquidation.
- Fills: tape model — resting level filled by an opposite-taker print
  at/through our price with size >= clip (we quote inside the spread,
  alone at our level). taker_side forced VARCHAR (duckdb bool-sniff
  gotcha, archived).
- End-of-day inventory: settlement truth from catalog_normal shards
  (sports settles same-day, CARRY expected rare — its rate is itself a
  reported diagnostic); missing truth -> CARRY at executable bid, never
  silently pooled.
- Primary endpoint: mean net c/contract over completed round trips,
  VALIDATE, per (sport, clip); cluster(=game) bootstrap x10k.
- Kill: no sport with positive VALIDATE mean whose CI excludes zero
  at clip=5, OR toxic:benign (STOP:PASSIVE_EXIT) > 3:1 in every sport.
- Promotion to Stage B ($200 live probe): at least one sport cell
  positive with CI excluding zero, n >= 100 clusters, and
  PASSIVE_EXIT share >= 25% of completed trips in that sport.

Biggest hole: no queue modeling at the touch — d>=1 keeps us alone at
our level so the tape fill is honest, but it means we never test
join-the-touch quoting, which is where S1's edge lived. If this run is
flat-negative while S1 was GO, the discrepancy points at touch-queue
economics and the next iteration needs the depth data.
