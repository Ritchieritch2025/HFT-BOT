# SPORTS-AUTORESEARCH-01 — operator mission text (VERBATIM ARCHIVE)

- received: 2026-07-13, pasted by the operator into a Cowork chat session
- status: **DRAFT — UNDER AUDIT, NOT RELEASED.** Archived per the CLAUDE.md
  full-text preservation rule (a pasted plan must never live only in chat).
  The audit is `docs/plan_audits/AUDIT_SPORTS_AUTORESEARCH_01_2026-07-13.md`.
  Nothing in this file authorizes any work; release requires a separate
  operator instruction after the audit findings are addressed.

---

## OPERATOR TEXT (VERBATIM)

OPERATOR MISSION — SPORTS-AUTORESEARCH-01
HYPOTHESIS FACTORY, MARKET-MECHANISM DISCOVERY, AND STRATEGY SHORTLIST
Repository:
  /Users/ritcardo/HFT BOT
Primary output:
  A comprehensive, reproducible research report that discovers, tests, rejects,
  and ranks patterns in Kalshi sports markets from:
  1. market-making;
  2. directional trading;
  3. relative-value and payout constraints;
  4. RFQ/combo flow;
  5. cross-market and cross-channel behavior;
  6. liquidity, regime, latency, and order-flow microstructure;
  7. genuinely novel anomalies unique to our data.
The final deliverable must give the operator a defensible foundation on which to
modify hypotheses and design strategies. It must not authorize or execute live
trading.
======================================================================
0. AUTHORITY GRANTED BY THIS MISSION
======================================================================
This mission authorizes:
- read-only research over W05-published S3 research releases;
- use of the operator-approved W09 research machine;
- research-only materialization, feature generation, replay, statistical
  analysis, charts, reports, hypothesis cards, and candidate dossiers;
- updates to research-owned ledgers and artifacts;
- commits of research code and reports to research-owned paths;
- exploratory investigation across every eligible sport and market family;
- formal confirmation only where the canonical phase/split authority already
  exists and can be proven from repository state.
This mission does NOT authorize:
- production EC2 access by SSH;
- reading production staging databases;
- capture, ingest, seal, sync, deployment, or service changes;
- modifying S3 objects;
- IAM changes;
- placing, canceling, modifying, or simulating real orders against Kalshi;
- posting an RFQ, responding to an RFQ, or submitting a quote;
- shadow/live trading;
- opening a frozen holdout without existing phase authority;
- paid external APIs, new datasets, machine resize, or new AWS spending without
  a separate operator approval;
- promotion to live trading.
This is one research mission, not a new governance project. Do not create a new
framework, constitution, release hierarchy, audit campaign, or planning loop.
Reuse the existing Research Workbench, mm_sandbox, Event Intelligence,
hypothesis ledger, and existing analysis doctrine. Add only the adapters and
research code actually required to complete the mission.
Do not ask for intermediate approval except for:
1. a new AWS spend action;
2. unavailable W09 capacity;
3. a material data-integrity failure;
4. a missing authority required to open a frozen validation/confirmation set.
Otherwise proceed autonomously.
======================================================================
1. EXECUTION GATES
======================================================================
Check these gates in order.
GATE A — DATA PLANE
Prove PIPE-W05 Phase-A is accepted and that a no-SSH consumer can:
- inventory;
- fetch;
- verify;
- resolve an immutable release_id;
- verify exact S3 VersionIds;
- verify the seal/publication state;
- access the required research data.
If the data plane is not accepted, return only:
  DATA_PLANE_GATE
  <one exact failed condition and its evidence>
Do not fall back to the dormant local Mac warehouse or direct EC2 reads.
GATE B — RESEARCH COMPUTE
Heavy scans, multi-day joins, raw RFQ materialization, L2 replay, bootstrap
campaigns, and multi-million-row studies must execute on W09.
Do not run them on:
- the Mac;
- the production EC2 capture machine;
- an unapproved new machine.
Use all CPU and memory available inside the currently approved W09 envelope,
subject to safe memory and disk controls below.
If W09 does not exist or a launch would create a new unapproved charge, return:
  W09_SPEND_GATE
Include only:
- recommended instance type;
- expected hourly price;
- estimated runtime;
- estimated total run cost;
- storage requirement;
- idle auto-shutdown setting.
Do not launch until approved.
GATE C — FORMAL RESEARCH AUTHORITY
Inspect:
- docs/PLAN_SPORTS_TRADING_PROGRAM_PROMPT_V2_2_CANDIDATE.md
- docs/PLAN_SPORTS_TRADING_DECISIONS.md
- docs/PLAN_SPORTS_TRADING_STATE.md
- docs/research_notes/HYPOTHESIS_LEDGER.md
- sandbox/research/workbench/hypotheses.json
The canonical prompt is pinned by path and SHA through D-2 even if its historical
filename/banner still contains CANDIDATE.
Determine the permitted mode:
MODE 1 — EXPLORATORY_AUTORESEARCH
Use this unless the repository contains valid authority for the relevant formal
phase and a hash-frozen, prior-exposure-aware split.
This mode may perform deep statistical exploration and candidate preparation,
but outputs must use:
  EXPLORATORY_ONLY
  DIAGNOSTIC_ONLY
  NOT A LIVE-TRADING AUTHORIZATION
It may produce PROMOTION_READY candidates but not VERDICT_PASS or live-ready
claims.
MODE 2 — GOVERNED_CONFIRMATION
Use only if the exact phase authority, prior-exposure ledger, and immutable split
are already valid.
This mode may perform TRAIN, VALIDATION, or HISTORICAL_CONFIRMATION only within
the authority actually present. Never infer authority from this mission.
Do not stop all exploratory work merely because formal confirmation authority is
absent. Continue in MODE 1 and prepare candidates and frozen test designs for
later confirmation.
======================================================================
2. FIRST ACTION AND REPRODUCIBILITY IDENTITY
======================================================================
Create:
  work/research/auto_research/<RUN_ID>/
where RUN_ID contains UTC time and the current code commit.
Archive this mission text once under the run directory and record its SHA-256.
Do not create a separate audit process for it.
Before analysis, freeze and record:
- repository commit;
- canonical prompt path and SHA;
- active W05 release_id for every selected date;
- exact S3 VersionId for every consumed object;
- seal and publication-state hashes;
- correction cutoff/state;
- schemas;
- code/config hashes;
- simulation implementation hash;
- feature-definition hash;
- random seeds;
- deterministic sort rules;
- W09 hardware and software environment;
- DuckDB version;
- Python/C++ compiler/runtime versions;
- UTC start time.
Every important result must be reproducible from these identities.
Required run artifacts:
  MISSION.md
  RUN_MANIFEST.json
  DATA_COVERAGE.json
  DATA_COVERAGE.md
  PRIOR_EXPOSURE.json
  FEATURE_DICTIONARY.json
  FEATURE_DICTIONARY.md
  TRIAL_REGISTRY.jsonl
  HYPOTHESIS_LEDGER.json
  HYPOTHESIS_LEDGER.md
  INCLUSION_EXCLUSION.csv
  RESOURCE_USAGE.json
  REPRODUCE.md
  reproduce.sh
  REPORT/index.html
  REPORT/EXECUTIVE_SUMMARY.md
  REPORT/FULL_REPORT.md
  REPORT/charts/**
  REPORT/tables/**
  CANDIDATE_DOSSIERS/**
  REJECTED_HYPOTHESES/**
  DATA_INTEGRITY/**
  queries/**
  logs/**
The trial registry is append-only within the run. Record failed, abandoned,
negative, and inconclusive trials—not only winners.
======================================================================
3. DATA SOURCE AND EVIDENCE DISCIPLINE
======================================================================
S3 W05 research releases are the only data source.
Use the existing research_data interface. Inspect its current help and manifest
contract rather than inventing flags or trusting stale README wording.
For every selected release:
1. inventory;
2. verify;
3. bind to release_id;
4. bind to exact VersionIds;
5. verify the seal/publication state;
6. inspect channel-level completeness;
7. inspect timestamp tier;
8. inspect capture gaps;
9. inspect sequence evidence;
10. inspect corrections;
11. inspect schema;
12. resolve the default active release by frozen publication/correction state,
    not local fetch time.
Default analysis view:
  SEALED_CONFIRMATION only.
Optional exploratory sensitivity view:
  SEALED_DEGRADED_EVIDENCE or quarantined historical data may be included only
  through an explicit exploratory selection. It must never be pooled silently
  with confirmation data. Every resulting artifact must be branded
  EXPLORATORY and report the reason for degradation.
Do not report "L2 available" globally. Determine it per release:
- INCLUDED_SEALED_FACTS; or
- ABSENT_FROM_THIS_RELEASE.
Do not use stale documentation claiming L2 is categorically unavailable if the
release manifest proves otherwise.
Inventory at least:
- catalog and dimensions;
- market lifecycle;
- scheduled start/close/settlement;
- fee class;
- trades;
- orderbooks_l1;
- sealed orderbooks_full L2, when present;
- RFQ raw communications data;
- seals;
- corrections;
- capture-gap evidence;
- TL1 evidence;
- sid/seq evidence by channel.
Produce a coverage cube by:
- date;
- hour;
- sport;
- league;
- root event;
- market family;
- market;
- channel;
- evidence tier;
- timestamp tier;
- fee-known/unknown;
- lifecycle completeness;
- gap status;
- sequence status.
For each cell report:
- bytes;
- rows/messages;
- root events;
- markets;
- first and last timestamp;
- missing intervals;
- excluded records;
- exclusion reasons.
A failed or unverified release is not exposed to analysis.
======================================================================
4. FOUR LABEL SYSTEMS — NEVER MIX THEIR MEANINGS
======================================================================
Every result must separately state:
A. DATA EVIDENCE
- SEALED_CONFIRMATION
- SEALED_DEGRADED_EVIDENCE
- CLOSED_HOUR_EXPLORATORY, only if later authorized
- EXPLORATORY_UNSEALED_LEGACY, where historically relevant
B. TIMESTAMP DISCIPLINE
- TL1
- PRE-TL1
- MIXED
C. EXPERIMENT SPLIT
- EXPLORATORY_ONLY
- TRAIN
- VALIDATION
- HISTORICAL_CONFIRMATION
D. ARTIFACT/CONCLUSION STATUS
Examples:
- DIAGNOSTIC_ONLY
- PROMOTION_READY
- COLLECT_MORE
- REJECTED
- DATA_STARVED
- VERDICT_PASS, only with formal authority
- VERDICT_FAIL, only with formal authority
Never use one label as evidence for another.
Existing dashboards, prior reports, and already examined periods are prior
exposure. At minimum, treat 2026-07-06 through 2026-07-10 and every event already
used in a dashboard, pilot, study, or human discussion as EXPLORATORY_ONLY until
the prior-exposure ledger proves otherwise.
======================================================================
5. TIME, CAUSALITY, AND SUB-SECOND RULES
======================================================================
Inventory every available clock:
- exchange event timestamp;
- websocket receipt wall clock;
- monotonic receive clock;
- decoder time;
- book-update/stack-processed time;
- feature-decision time;
- simulated send time;
- simulated venue-arrival time;
- fill-observation time.
Use the last timestamp genuinely available to the strategy as its decision
clock. Prefer the proven local receive/processed clock for tradable replay.
Do not reorder history using a timestamp that would not yet have been available
to the strategy.
All joins are strict as-of joins.
No feature may observe:
- a later message;
- a future trade;
- a future score;
- a future book state;
- an eventual RFQ deletion before it occurs;
- a final event classification unavailable at decision time;
- future settlement;
- a corrected catalog value not yet available then.
Sub-second analysis:
- TL1-era data must support raw-event 1ms, 10ms, and 100ms windows;
- do not add a sampling layer;
- compute from raw events;
- PRE-TL1 data is excluded from millisecond-level claims;
- show inter-arrival and processing-latency distributions;
- report burst aliasing and timestamp ties;
- report p50, p90, p99, p99.9, max, and n where applicable.
If score/game-state data is not available with valid as-of timestamps, do not
invent or infer exact game states from price alone. Time-to-start and market
behavior may still be studied; score-conditioned claims must remain unavailable.
======================================================================
6. COMPUTE AND RESOURCE CONTRACT
======================================================================
All heavy work executes on W09.
For every DuckDB connection:
- set an explicit memory_limit;
- set an explicit threads value;
- set a bounded temp directory;
- enable partition pruning;
- select only needed columns;
- avoid full unbounded materialization;
- stream or batch large raw files;
- close the connection deterministically.
Set the total working memory target to no more than approximately 70% of
available W09 RAM unless the machine's approved operating policy specifies a
lower cap. Leave headroom for the OS, decompression, caches, and report building.
Use a research funnel:
1. manifest and metadata scan;
2. bounded representative sample;
3. one market family;
4. one sport;
5. cross-sport expansion;
6. full confirmation only for survivors.
Cache immutable intermediate data by:
- release_id;
- VersionId set;
- query hash;
- feature hash;
- code hash.
Never cache under a key that omits data identity.
Track:
- CPU-hours;
- peak RAM;
- temporary disk;
- cached bytes;
- S3 bytes read;
- wall time;
- failed/retried tasks;
- estimated AWS cost;
- actual AWS cost where available.
Use the approved W09 machine fully, but do not resize it or create additional
machines without a new spend approval.
Configure idle auto-shutdown after the approved idle interval. Confirm shutdown
at mission completion.
======================================================================
7. UNIVERSE CONSTRUCTION
======================================================================
Start with all eligible sports. Do not preselect Baseball, Tennis, Soccer,
Basketball, or any league merely because a previous dashboard used them.
Construct the research universe event-first:
  sport
    → league/competition
      → root event
        → market family
          → market
            → channel observations
Freeze and report:
- universe inclusion rules;
- lifecycle requirements;
- title/root-event mappings;
- market-family definitions;
- mutually exclusive/exhaustive family status;
- scheduled-start availability;
- pre-match vs in-play classification;
- fee status;
- settlement/rule status;
- excluded markets and reasons.
Run broad discovery across all eligible sports, then stratify by:
- sport;
- league;
- market family;
- pre-match/in-play;
- fee class;
- price band;
- time to scheduled start;
- liquidity regime;
- message-rate regime;
- spread regime;
- depth regime;
- volatility regime;
- event popularity;
- day of week;
- hour of day.
The primary program direction remains selective pre-match market-dynamics spread
capture. In-play results are diagnostic unless separately authorized.
Directional Track D findings remain proposal-only. RFQ/combo research is
read-only. External-odds research remains deferred unless separately authorized.
======================================================================
8. FEATURE DICTIONARY
======================================================================
Create a versioned feature dictionary before relying on any feature.
Each feature entry must contain:
- exact name;
- plain-language definition;
- formula;
- units;
- sign convention;
- source fields;
- source channel;
- timestamp used;
- lookback window;
- minimum observations;
- gap behavior;
- null behavior;
- causal availability;
- PRE-TL1/TL1 eligibility;
- implementation location;
- tests.
At minimum construct and test:
MARKET STATE
- best bid and ask;
- executable spread;
- midpoint;
- microprice, when L2 exists;
- price level;
- depth at each level;
- cumulative depth at N levels;
- depth slope/convexity;
- bid/ask imbalance;
- order-flow imbalance;
- signed trade flow;
- marketable volume;
- trade intensity;
- quote/update intensity;
- cancellation/depletion proxy;
- realized volatility;
- jump size;
- quote age;
- stale-book age;
- one-sided/crossed/locked status.
TIME
- time since listing;
- time to scheduled start;
- time since last trade;
- time since last book update;
- time since liquidity depletion;
- time since large-flow event;
- time since RFQ creation;
- RFQ age/lifetime;
- time since regime transition.
BURSTS AND RESILIENCE
- message inter-arrival time;
- burst count in 1/10/100ms and coarser windows;
- depth depletion;
- refill time;
- refill fraction;
- spread widening;
- spread recovery;
- price-jump hazard after depletion;
- calm-to-burst transition probability.
RELATIVE VALUE
- family executable-price sum;
- payout-constraint residual;
- monotonicity residual;
- combo indicative price vs executable leg cost;
- within-event relative dislocation;
- lagged family-price response.
INVENTORY AND FACTORS
For simulated strategies:
- market inventory;
- root-event payout-state exposure;
- sport/league concentration;
- correlated-family exposure;
- capital locked;
- worst-state payout;
- factor/family imbalance.
Every formula must have unit tests and sign-direction tests.
======================================================================
9. HYPOTHESIS FACTORY
======================================================================
Use this canonical hypothesis grammar:
"In [frozen population], when [observable state at decision time] occurs,
[specified executable action or future observable] changes [named outcome] over
[horizon] in [direction], after [cost/fill assumptions], compared with
[control]."
Each hypothesis card must include:
- hypothesis_id;
- one falsifiable sentence;
- mechanism;
- population;
- unit of analysis;
- decision clock;
- feature;
- treatment/event definition;
- control;
- horizon;
- expected direction;
- primary metric;
- secondary metrics;
- economic relevance threshold;
- fee model;
- fill model;
- exclusions;
- split;
- test family;
- multiplicity family;
- sample-size/power requirement;
- rejection criterion;
- reopen condition;
- data/timestamp/split/artifact labels;
- required engine capabilities;
- code/data references.
Per cycle:
- generate no more than 10 new hypotheses;
- include at least two hypotheses originating from anomalies in our own data;
- those two must not simply restate a public textbook/playbook effect;
- show the plot or distribution that triggered each novel hypothesis;
- explain why a generic quant bot scanning public strategies would not discover
  it without our particular channels, market structure, or event linkage;
- rank anomaly-driven ideas highly on information gain;
- do not use model complexity as a substitute for originality.
Ingest existing registered hypotheses rather than silently duplicating them:
- H-OP-1 / H-14 favorite miscalibration;
- H-OP-2 favorite erosion;
- ATL-TTS-01;
- ATL-BURST-01;
- ATL-TOX-01;
- ATL-RES-01;
- ATL-FLOW-01;
- ATL-WINDOW-01;
- fee-wall hypotheses;
- toxicity concentration;
- size-toxicity;
- queue hypotheses;
- event correlation;
- retail/uninformed-flow proxies;
- one-sided flow;
- iceberg/replenishment;
- anchor manipulability.
Existing Workbench cards are drafts, not frozen proof. Reconcile aliases and
link them into one trial/ledger identity.
Mandatory seed studies:
1. THREE-WAY-OVERROUND
   Re-measure the USA–Belgium-style pilot across all valid match-winner families.
   Use executable prices, not display midpoints.
   Include:
   - simultaneous quote availability;
   - executable side;
   - fee-adjusted residual;
   - persistence;
   - fill feasibility;
   - time-to-start;
   - sport/league stratification;
   - event clustering;
   - false-discovery control.
   The prior "103–104 cents for roughly 9% of joint minutes" observation is only
   a pilot and must not be treated as established.
2. SOCCER-POISSON-RV
   Test same-event family consistency and structural residuals using strictly
   observable market inputs. Do not smuggle final scores into the feature set.
   Compare residual persistence, executable correction, fees, and capacity.
3. LARGE-FLOW-CONTINUATION
   Never call a large L1/trade event a "whale identity." L1/trades do not identify
   an actor.
   Test whether large observable flow predicts:
   - continuation;
   - reversal;
   - spread widening;
   - depth depletion;
   - refill;
   - adverse selection;
   - regime change;
   over multiple horizons and conditional regimes.
======================================================================
10. MARKET ATLAS — BASELINE BEFORE STRATEGY SEARCH
======================================================================
Build STUDY-SPORT-ATLAS-01 first.
For every sport, league, event family, and time-to-start segment, characterize:
- market availability;
- number of listed markets;
- active-market share;
- trade activity;
- message rate;
- inter-arrival time;
- spread;
- top-level and multi-level depth;
- price-band occupancy;
- volatility;
- jump frequency;
- one-sided duration;
- book staleness;
- liquidity concentration;
- time-to-first/last trade;
- listing and close rhythms;
- fee class;
- pre-match/in-play behavior;
- root-event market-family breadth;
- L2 and RFQ coverage where present.
Use event-first map/reduce. Do not let sports with millions of messages dominate
sports with fewer events.
Produce per-event summaries first, then aggregate across root events.
The atlas must reveal where each later strategy is actually executable, not
merely where a statistical pattern exists.
======================================================================
11. MARKET-MAKING RESEARCH
======================================================================
Evaluate the following mechanisms.
A. SPREAD CAPTURE
Measure gross spread opportunity and net realized opportunity after:
- fees;
- adverse markout;
- fill probability;
- partial fills;
- cancel latency;
- replacement latency;
- inventory drift;
- forced/terminal exit;
- capital lock;
- unhedged root-event payout exposure.
A visible spread is not profit.
B. TOXICITY AND MARKOUTS
Compute signed markouts at:
- 10ms;
- 25ms;
- 50ms;
- 100ms;
- 250ms;
- 500ms;
- 1s;
- 2s;
- 5s;
- 10s;
- 30s;
- 120s,
only where timestamp evidence supports the horizon.
Condition markouts on:
- spread;
- price band;
- imbalance;
- OFI;
- message burst;
- trade burst;
- recent jump;
- depth;
- depletion;
- refill;
- quote age;
- time to start;
- sport;
- market family;
- fee class;
- large-flow events;
- RFQ activity;
- regime.
C. DEPLETION AND REFILL
Measure:
- probability of refill;
- refill time;
- refill fraction;
- spread recovery;
- price-jump probability before refill;
- same-side vs opposite-side refill;
- repeated depletion;
- resilience by sport and market family.
Use survival curves or competing-risk analysis where appropriate, including
right-censored observations.
D. LIQUIDITY REGIMES
Discover interpretable regimes such as:
- dormant;
- calm/tight;
- active/tight;
- active/wide;
- burst/toxic;
- depleted;
- recovery;
- one-sided;
- stale/disconnected.
Start with transparent threshold/state models. Complex clustering or HMMs may be
used only if they materially improve out-of-sample stability and remain
interpretable.
E. QUOTING POLICY ABLATION
Run a common-random-number comparison of the existing DP ladder:
- DP-0 static touch;
- DP-1 burst/toxicity eligibility;
- DP-2 dynamic spread;
- DP-3 dynamic fair value;
- DP-4 inventory skew;
- DP-5 requote policy;
- DP-6 dynamic size, diagnostic/capacity only unless separately authorized.
Report paired per-root-event changes. Each rung must justify its incremental
complexity. A more complex rung that does not improve the frozen primary metric
and robustness is rejected.
F. QUEUE MODEL
The binding fill authority is strict-through one-contract replay unless a
separately certified own-order calibrated model exists.
Rules:
- at-price trades do not fill strict-through;
- only strict price-through qualifies;
- pending cancel remains exposed until simulated cancel arrival;
- gaps and ambiguity resolve against the strategy;
- no future state;
- one-contract binding;
- queue-aware L2 output is diagnostic only until independently calibrated.
Compare strict-through and queue-aware results, but never let the queue model
silently rescue an unprofitable strict result.
G. INVENTORY AND STATEWISE PAYOUT
Test whether apparently independent market-making positions concentrate into the
same root-event payout state.
Calculate:
- inventory by market;
- family-level exposure;
- worst-state payout;
- correlation/factor concentration;
- capital hours;
- inventory half-life;
- forced-exit cost;
- tail exposure.
======================================================================
12. DIRECTIONAL RESEARCH
======================================================================
Directional research is proposal-only unless separately authorized.
Evaluate:
- short-horizon continuation;
- short-horizon mean reversion;
- post-jump continuation/reversal;
- post-large-flow continuation/reversal;
- OFI predictivity;
- imbalance predictivity;
- microprice predictivity;
- depletion-before-jump;
- refill-before-reversion;
- volatility breakout;
- calm-to-burst transition;
- favorite-longshot calibration;
- favorite erosion by time-to-start;
- listing-to-start drift;
- within-event lead/lag;
- related-market propagation;
- market-family dislocation correction;
- RFQ-triggered CLOB response.
For a directional taker:
- enter at executable ask/bid;
- include exact fee;
- include latency;
- include slippage;
- include unavailable-size effects;
- include exit cost;
- prohibit midpoint entry/exit assumptions.
Report forecast calibration separately from trading profitability. A statistically
predictive return that cannot overcome the bid/ask spread, fees, latency, and
capacity is rejected as a trading strategy.
Use multiple horizons and show signal decay. Do not select the best horizon after
looking without accounting for it as another trial.
======================================================================
13. RELATIVE-VALUE AND PAYOUT-CONSTRAINT RESEARCH
======================================================================
Test:
- mutually exclusive family sums;
- exhaustive family sums;
- three-way match-winner overround/underround;
- bracket monotonicity;
- threshold-market monotonicity;
- same-match family consistency;
- cross-market lead/lag;
- event-family stale-leg behavior;
- combo vs executable leg cost;
- payout-state arbitrage constraints;
- family residual persistence and correction.
All legs must be simultaneously observable at the decision clock.
Report:
- raw residual;
- executable residual;
- fee-adjusted residual;
- size available;
- asynchronous-leg risk;
- hedge slippage;
- residual lifetime;
- event concentration;
- real capacity.
Do not call a constraint violation tradable if one leg is stale, unavailable, or
too small.
External-venue anchors are excluded unless separately authorized and accompanied
by as-of timestamp evidence.
======================================================================
14. RFQ RESEARCH
======================================================================
Treat RFQ as a broadcast-flow and market-intent channel, not as observed RFQ
pricing or profitability.
Expected official communications events include:
- rfq_created;
- rfq_deleted.
Validate field names against observed schemas and the stored source contract.
Do not use memory-based field assumptions.
Relevant fields may include:
- id;
- creator_id;
- market_ticker;
- event_ticker;
- created_ts;
- deleted_ts;
- contracts_fp;
- target_cost_dollars;
- mve_collection_ticker;
- mve_selected_legs.
Selected legs may include:
- event_ticker;
- market_ticker;
- side;
- yes_settlement_value_dollars.
Build research-only provisional derived tables on W09 if they do not exist:
1. rfq_requests
2. rfq_lifecycle
3. rfq_legs
4. rfq_clob_context
Do not freeze the schema merely because it was drafted in advance. First inspect
the actual 48-hour flow and record field presence, null rates, schema variation,
and joinability.
RFQ identity discipline:
- join create/delete by RFQ id;
- first valid delete after create is the lifecycle endpoint;
- treat unmatched creates as right-censored;
- treat unmatched deletes separately;
- hash requester IDs in research outputs;
- do not claim identity where creator_id is empty;
- report requester-ID coverage;
- do not infer one person from static IDs without proof.
RFQ completeness discipline:
- communications broadcasts may not provide a mandatory usable per-event seq;
- do not claim sequence completeness without evidence;
- report channel-level connection/drop/gap evidence;
- distinguish capture completeness from lifecycle join completeness.
Mandatory RFQ analyses:
A. FLOW
- requests per minute/hour/day;
- create/delete rates;
- RFQ storms/bursts;
- inter-arrival time;
- time-of-day and day-of-week;
- sport/category/market-family distribution;
- pre-match/in-play classification;
- event concentration.
B. SIZE AND ECONOMIC INTENT
- contracts_fp distribution;
- target_cost_dollars distribution;
- recurring round-number sizes;
- size by category;
- size by combo/single;
- size by time-to-start;
- size by requester-hash where available.
C. LIFECYCLE
- create-to-delete lifetime;
- right-censoring;
- repeat create/delete cycles;
- repeated RFQ IDs;
- rapid replacement behavior;
- lifecycle by size, sport, family, and regime.
D. COMBOS
- known-combo share;
- combo/single ratio;
- leg-count distribution;
- selected-side distribution;
- repeated leg bundles;
- cross-event vs same-event structure;
- collection concentration;
- leg-market liquidity;
- executable indicative cost of legs;
- combo structural residual.
If combo recognition is incomplete, report known-HVM share as a lower bound, not
the exact HVM share.
E. REQUESTER CONCENTRATION
Where IDs exist:
- requests per hashed requester;
- size per requester;
- category breadth;
- repeat interval;
- Herfindahl/concentration;
- Lorenz curve;
- top-share sensitivity;
- requester coverage.
Never refer to a hashed requester as a known actor or person.
F. RFQ–CLOB RELATIONSHIP
Use as-of joins to L1/L2/trades around each RFQ.
Measure CLOB state before and after RFQ creation/deletion:
- spread;
- depth;
- imbalance;
- message rate;
- signed trades;
- volatility;
- jump probability;
- depletion;
- refill;
- related-leg movement.
Use event-study windows such as:
- -30s to -10s;
- -10s to -1s;
- -1s to 0;
- 0 to 100ms;
- 100ms to 1s;
- 1s to 3s;
- 3s to 10s;
- 10s to 30s;
- 30s to 120s,
subject to valid clocks.
Compare with matched non-RFQ control periods having similar:
- sport;
- market family;
- time-to-start;
- price band;
- spread;
- depth;
- activity regime;
- hour of day.
RFQ hard truth:
The broadcast data does not necessarily contain the quote price, accepted quote,
winning quote, or fill. Therefore do not report:
- RFQ acceptance rate;
- RFQ fill rate;
- quote hit rate;
- actual RFQ maker PnL;
- actual quote competitiveness;
- winner's-curse estimates requiring an observed accepted quote;
unless those fields genuinely exist in another verified dataset.
You may report indicative CLOB/leg economics only as a clearly labeled proxy.
======================================================================
15. STATISTICAL TEST LADDER
======================================================================
Every hypothesis moves through the following ladder.
TIER 1 — DESCRIPTIVE DISCOVERY
Ask whether the phenomenon exists.
Report:
- effect distribution;
- frequency;
- persistence;
- population;
- heterogeneity;
- sample size;
- root-event count;
- day blocks;
- missingness;
- selection/exclusions;
- economic scale.
Discoveries made while browsing are Tier 1 only. They must be registered and
tested again from scratch before promotion.
TIER 2 — FREEZE
Before computing a verdict, freeze:
- hypothesis;
- population;
- dates;
- split;
- feature definition;
- primary metric;
- economic threshold;
- fill model;
- fee model;
- latency model;
- exclusions;
- multiplicity family;
- test;
- seed;
- rejection rule;
- COLLECT_MORE rule.
Write the frozen card before opening the corresponding outcome data.
TIER 3 — TRAIN
Where authorized:
- develop on chronological TRAIN only;
- use nested chronological folds;
- keep root events intact;
- purge overlapping labels;
- embargo by at least maximum feature window plus maximum outcome horizon;
- fit feature/model/threshold/fill parameters within each training fold;
- register every tried variant.
TIER 4 — VALIDATION
Open once only, under authority.
After opening, that set is no longer untouched. Do not retune and continue
calling it validation.
TIER 5 — HISTORICAL CONFIRMATION
Use only with explicit authority and an untouched sealed period.
A candidate cannot earn a formal verdict from PRE-TL1-only or dev-grade data
when its claimed edge depends on milliseconds, queue, or latency.
======================================================================
16. UNIT OF INFERENCE AND PRIMARY ECONOMIC ESTIMAND
======================================================================
Rows, websocket messages, quote changes, and fills are not independent samples.
Aggregate strategy outcomes by root event:
  NetPnL_e = total net strategy PnL for root event e,
             including fees, exits, zero-fill events, and losses.
The primary economic estimand is:
  mu_event = E[NetPnL_e]
Include:
- events where the strategy did not quote;
- events where it quoted but did not fill;
- events with zero trades;
- events with rejected eligibility;
- negative and zero outcomes.
Primary uncertainty:
- complete-root-event calendar-day block bootstrap;
- at least 1,000 replicates for exploratory runs;
- preferably 5,000 deterministic replicates for finalists;
- preserve events within day blocks;
- report 95% interval and bootstrap distribution.
A formal positive economic conclusion requires:
  ci95_lower(mu_event) > 0
and the lower bound must also exceed the predeclared minimum economically
relevant effect.
Also report:
- median event PnL;
- p5/p1 event PnL;
- expected shortfall;
- max event loss;
- max day loss;
- drawdown;
- capital hours;
- turnover;
- fill count;
- events quoted;
- events filled;
- fee class × layer results;
- capacity.
Never merge fee/fill layers to manufacture sample size. If a layer has fewer
than 200 relevant root events, return COLLECT_MORE unless its frozen power
analysis justifies another threshold.
======================================================================
17. MULTIPLE TESTING AND ANTI-SELF-DECEPTION
======================================================================
Maintain a complete trial count.
For broad discovery:
- group related hypotheses into declared families;
- apply Benjamini–Hochberg FDR within each family;
- report raw and adjusted values;
- show survivor count before and after adjustment.
For the formal shortlist:
- freeze K candidates;
- use Holm correction across the K primary tests;
- report unadjusted and adjusted inference.
Where suitable, include supportive selection-bias diagnostics such as:
- deflated Sharpe;
- probability of backtest overfitting;
- Reality Check or SPA-style tests.
These do not replace event-level economic confidence intervals.
Required negative controls:
- future/time-shifted signal;
- sign flip;
- event-label shuffle;
- market-label shuffle;
- random feature of matched dimension;
- unrelated-event control;
- delayed execution;
- impossible look-ahead sentinel;
- same analysis on periods where the mechanism should not operate.
If a negative control "works," investigate leakage before trusting the real
signal.
No sample-until-significant behavior.
If the frozen sample is underpowered, return COLLECT_MORE with the required
additional root-event count. Do not extend the window opportunistically and
reuse the original alpha.
======================================================================
18. EXECUTION AND FILL ECONOMICS
======================================================================
For every candidate, distinguish:
- predictive signal;
- quote opportunity;
- executable order;
- simulated fill;
- realized net PnL.
Fees:
- use exact fee facts by date, product, side, price, and liquidity role;
- report fee provenance;
- exclude unknown-fee observations from profitability gates;
- include them only in explicitly labeled descriptive analysis.
Maker binding case:
- certified strict-through;
- one contract;
- no at-price fills;
- cancel remains exposed until simulated arrival;
- gaps/ambiguity resolve against the strategy;
- no future;
- exact fixed-point fees.
Directional/taker case:
- cross executable bid/ask;
- cap by displayed executable size;
- include fee, latency, and slippage;
- model non-synchronous hedge legs;
- model terminal exit.
Run at least:
- optimistic diagnostic;
- base;
- conservative binding.
Promotion uses conservative, not optimistic.
Required stress cases:
- latency ×1.5;
- latency ×2;
- latency ×5 diagnostic;
- fee increase;
- fill probability reduction;
- queue-ahead p90;
- cancel p99 ×2;
- spread widening;
- displayed-depth reduction;
- market gap;
- disconnect;
- correlated inventory shock;
- forced close;
- top-liquidity events removed.
======================================================================
19. ROBUSTNESS, CAPACITY, AND CONCENTRATION
======================================================================
For every serious candidate report:
- chronological fold stability;
- sport stability;
- league stability;
- market-family stability;
- price-band stability;
- time-to-start stability;
- fee-layer stability;
- regime stability;
- parameter-neighborhood stability;
- effect decay;
- alternative reasonable feature definitions;
- alternative gap exclusions;
- PRE-TL1 exclusion sensitivity;
- degraded-release exclusion sensitivity.
Perform:
- delete-best-day;
- delete-best-root-event;
- delete-top-5 events;
- delete-highest-volume sport;
- leave-one-sport-out;
- leave-one-league-out;
- leave-one-week-out where coverage permits.
Report concentration by:
- day;
- root event;
- market;
- sport;
- league;
- strategy;
- fill;
- RFQ requester hash where relevant.
Capacity analysis:
- net expectancy vs size;
- fill vs size;
- depth consumed;
- participation;
- queue depletion;
- capital locked;
- number of simultaneous events;
- factor/payout-state concentration;
- expected and worst-case inventory.
A candidate dominated by one event, one day, or an implausible amount of
displayed liquidity is not promotion-ready.
======================================================================
20. VISUALIZE-EVERYTHING CONTRACT
======================================================================
No scalar statistic may be reported alone.
For every continuous statistic provide:
- histogram or ECDF;
- p50;
- p99;
- max;
- n.
Add other percentiles where useful.
For shares and counts provide:
- distribution across the natural unit, such as per hour, root event, sport, or
  day; or
- bootstrap confidence-interval plot.
Every chart must include:
- title;
- evidence-tier banner;
- timestamp tier;
- split;
- plain-language definition;
- formula;
- units;
- provenance;
- release/date coverage;
- n rows;
- n markets;
- n root events;
- n day blocks;
- fee/fill assumptions;
- code location;
- exclusions;
- uncertainty;
- caption.
Captions must explicitly name and explain:
- bimodality;
- long tails;
- discontinuities;
- zero inflation;
- censoring;
- truncation;
- regime mixtures;
- suspicious spikes.
Mandatory chart families:
DATA TRUTH
- release/channel/date coverage heatmap;
- TL1/PRE-TL1 matrix;
- gap timeline;
- sid/seq availability;
- exclusions waterfall.
MARKET ATLAS
- spread ECDFs;
- depth ECDFs;
- message-rate distributions;
- inter-arrival distributions;
- time-to-start liquidity maps;
- price-band occupancy;
- per-sport/per-family forest plots.
MICROSTRUCTURE
- synchronized regime timeline;
- L2 depth heatmaps;
- depletion/refill survival curves;
- markout fan charts;
- OFI/imbalance calibration;
- burst drill-down at 1/10/100ms;
- calm-to-burst transition plots;
- spread/depth resilience.
DIRECTIONAL
- event-study paths;
- continuation/reversal curves;
- signal calibration;
- horizon decay;
- lead/lag correlograms;
- executable net-return distributions.
RELATIVE VALUE
- family residual distributions;
- overround/underround heatmaps;
- residual persistence;
- executable residual after fees;
- simultaneous-size capacity.
RFQ
- hourly/day heatmap;
- inter-arrival ECDF;
- size and target-cost distributions;
- lifetime survival curve;
- combo leg-count distribution;
- category/time-to-start mix;
- hashed-requester Lorenz/concentration;
- RFQ–CLOB event studies;
- indicative combo-vs-leg residuals;
- censoring and requester-ID-coverage charts.
STRATEGY
- per-event PnL distribution;
- bootstrap mu_event distribution;
- underwater/drawdown;
- capacity curves;
- concentration;
- stress-test tornado;
- DP-0 through DP-6 paired ablation;
- sensitivity surfaces;
- fold-by-fold forest plot.
Produce a navigable offline HTML report using existing dashboard/workbench
components where practical. Do not create another dashboard framework merely to
render the report.
======================================================================
21. INDEPENDENT CROSS-CHECK FOR FINALISTS
======================================================================
For each finalist, implement a clean-room cross-check that does not call the
primary aggregation function.
At minimum independently recompute:
- population count;
- root-event count;
- feature/treatment count;
- primary effect;
- NetPnL_e;
- mu_event;
- fees;
- fill count;
- confidence interval.
The two implementations must agree within a declared tolerance.
Run critical analyses twice using deterministic seeds and ordering. Compare
artifact hashes.
Any unexplained disagreement blocks promotion and becomes a bug investigation.
======================================================================
22. HYPOTHESIS STATUS AND STOP RULES
======================================================================
Allowed hypothesis statuses:
- CANDIDATE
- DESCRIPTIVE_SURVIVOR
- FROZEN
- TRAIN_SURVIVOR
- PROMOTION_READY
- VERDICT_PASS, only when formally authorized
- VERDICT_FAIL, only when formally authorized
- COLLECT_MORE
- DATA_STARVED
- REJECTED
- INVALIDATED_BY_DATA
- INVALIDATED_BY_LEAKAGE
Every rejected hypothesis must record:
- what evidence killed it;
- whether it was statistically absent, economically too small, non-executable,
  unstable, too concentrated, or data-invalid;
- conditions under which it may reopen.
Continue autonomous cycles until the first applicable terminal condition:
1. four formal VERDICT_PASS candidates exist, if formal confirmation is
   authorized;
2. two to four strong PROMOTION_READY candidates exist and the next step requires
   a frozen validation authority;
3. two consecutive cycles produce zero descriptive survivors;
4. the remaining candidates are all COLLECT_MORE or DATA_STARVED;
5. a core data-integrity anomaly invalidates the shared dataset;
6. additional progress requires new spending.
A problem isolated to one channel/date/sport should quarantine that subset and
allow unrelated valid work to continue. Do not stop the entire mission for a
non-core missing channel.
======================================================================
23. CANDIDATE PROMOTION DOSSIER
======================================================================
Produce one dossier per shortlisted strategy.
Each dossier must contain:
1. NAME AND STATUS
2. ONE-SENTENCE HYPOTHESIS
3. ECONOMIC MECHANISM
4. WHY THE EDGE MAY PERSIST
5. EXACT MARKET/SPORT/REGIME POPULATION
6. EXACT CAUSAL FEATURE FORMULAS
7. DECISION CLOCK
8. ENTRY, QUOTE, CANCEL, EXIT, AND HEDGE PSEUDOCODE
9. DATA COVERAGE AND EVIDENCE TIERS
10. FROZEN TEST DESIGN
11. TRIAL COUNT AND MULTIPLICITY ADJUSTMENT
12. DESCRIPTIVE RESULTS
13. NET ECONOMIC RESULTS
14. EVENT-LEVEL CONFIDENCE INTERVALS
15. FILL AND FEE MODEL
16. LATENCY SENSITIVITY
17. PARAMETER SENSITIVITY
18. SPORT/REGIME/FOLD HETEROGENEITY
19. TAILS AND DRAWDOWN
20. CONCENTRATION
21. CAPACITY
22. NEGATIVE CONTROLS
23. CLEAN-ROOM CROSS-CHECK
24. REQUIRED ENGINE CAPABILITIES
25. CURRENT ENGINE CAPABILITY GAPS
26. FAILURE MODES
27. REOPEN/COLLECT-MORE CONDITIONS
28. PROPOSED MICRO-LIVE DESIGN
29. KILL CONDITIONS
30. SUCCESS/ABORT METRICS
31. EXPLICITLY UNAUTHORIZED BANNER
The proposed micro-live section is a design proposal only. It does not authorize
the experiment.
Rank candidates using disclosed raw components:
- conservative CI lower bound;
- capacity;
- persistence/moat;
- robustness;
- implementation difficulty;
- operational risk;
- data quality.
If presenting a composite score, disclose the normalization and weights. Never
hide raw components behind one score.
======================================================================
24. FINAL REPORT
======================================================================
The report must be comprehensive enough for the operator to audit, modify, and
build strategies from.
Required sections:
1. Executive conclusion
2. What data was actually available
3. What data was unavailable
4. Evidence/timestamp/split labels
5. Data-integrity findings
6. Market atlas
7. Regime atlas
8. Market-making findings
9. Directional findings
10. Relative-value findings
11. RFQ findings
12. Novel anomalies unique to our data
13. Hypothesis trial registry summary
14. Multiple-testing correction
15. Rejected hypotheses and why they died
16. Data-starved hypotheses
17. Strategy shortlist
18. Detailed candidate dossiers
19. Capacity and infrastructure implications
20. Required additional data
21. Required engine improvements
22. Recommended next experiments
23. Resource/cost report
24. Full reproducibility instructions
25. Limitations and prohibited interpretations
The executive summary must clearly separate:
- pattern exists;
- predicts something;
- is executable;
- survives costs;
- survives conservative fills;
- survives unseen data;
- is authorized for live testing.
These are different claims.
No vague language such as:
- "looks promising";
- "appears profitable";
- "likely edge";
without exact evidence, uncertainty, execution assumptions, and status.
======================================================================
25. FINAL RESPONSE DISCIPLINE
======================================================================
Do not send watcher messages, routine progress, or operational chatter.
Only interrupt the operator for:
- DATA_PLANE_GATE;
- W09_SPEND_GATE;
- DATA_INTEGRITY_BLOCKER;
- missing formal authority needed to open a frozen set.
Otherwise complete the mission autonomously.
On completion, return:
  AUTORESEARCH_COMPLETE
and only the following summary:
- run_id;
- report path;
- code commit;
- exact data releases/date coverage;
- number of hypotheses tested;
- number rejected;
- number COLLECT_MORE/DATA_STARVED;
- number PROMOTION_READY;
- number formal VERDICT_PASS, if authorized;
- shortlisted strategy names;
- W09 runtime and cost;
- highest-priority data/engine gap;
- explicit statement that no live trading was authorized or executed.
Then stop.
