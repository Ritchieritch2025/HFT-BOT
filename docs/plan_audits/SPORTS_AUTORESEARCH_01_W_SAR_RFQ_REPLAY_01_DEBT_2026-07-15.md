# W-SAR-RFQ-REPLAY-01 — deferred RFQ descendant-replay wiring debt

Status: `DEFERRED_NOT_AUTHORIZED`  
Source run: `20260715T112538Z__c21a79a8cff__deep01`  
Recorded because of operator stop order: 2026-07-15T21:47:04Z

## Problem statement

repair-07 formal execution replayed the chain 07→06→05→04. The repair-04 and
repair-05 validators selected top-level mutable `RFQ_FULL_STAGE_STATE.json` /
`RFQ_FULL_INPUT_IDENTITY.json` instead of the corresponding descendant
`pre_repair` snapshot. The comparison therefore failed before a new RFQ state,
input identity, scratch database, WAL or result was written.

Observed error:

`repair-04 active/archive evidence differs: REPORT/tables/RFQ_FULL_STAGE_STATE.json`

Terminal resource receipt:

- path: `logs/resources/rfq_full_stage_repair07.json`
- SHA-256: `7dc5271d1588b7f797c9c7126d11a59b7c92f03712fc3bdbc044fcf72c165124`
- return code: 1
- wall seconds: 218.733

## Archived proposal only

If a future operator explicitly reopens this W, the smallest proposed change is:

1. During descendant replay, read only repair-04/05 active state and input identity
   from the exact descendant `pre_repair` snapshot.
2. Continue validating blocker, resource, scratch receipt, Cycle-1 binding and all
   approved archive hashes from their immutable registered locations.
3. Append a new repair record and a new resource label; never overwrite repair-07
   evidence.
4. Add an unmocked multi-level replay regression that exercises 08→07→06→05→04.

## Stop boundary

This document is an engineering-debt archive, not an implementation plan or
authorization. The operator expressly ordered: no repair-08, no further repair,
audit, test or RFQ computation. The two-day dataset makes additional RFQ work
economically unjustified for this run.
