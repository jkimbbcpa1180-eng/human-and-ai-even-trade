# Substrate v9 — basic-needs pool + receipt-bound contributions

Credit: humanity. Released to the public domain under CC0-1.0.

A simulation/policy prototype: not deployed AGI, not a financial product, not a
secure distributed ledger, and not a guarantee that any resource exists.

**Where it came from:** the project owner designed the v9 changes with GPT,
which delivered them as a runtime patch installer. That patch was reviewed,
run against the real v8 file, and rewritten here as plain source (no `exec`,
no runtime class swapping), with a few extra fixes from the review.

## Run

```sh
python substrate_v9.py --test          # built-in checks (v7/v8 suite + v9 integration)
python substrate_v9.py --demo          # full demo (see demo_output_v9.txt)
python substrate_v9.py --basic-needs   # JSON of the v9 integration run
python substrate_v9.py --pod-report    # pod model (unchanged from v8)
python substrate_v9.py --municipal-demo
python -m unittest test_substrate_v9 -v
```

`v9.patch` turns `substrate_v8.py` into `substrate_v9.py` byte for byte
(`patch substrate_v8.py v9.patch`). Standard library only.

## What changed from v8

1. **BasicNeedsPool (new).** Physical inventory kept apart from TC, in four
   integer units: `food_kcal`, `water_ml`, `energy_wh`, `shelter_person_days`.
   Every active registered person gets an equal share each ledger day. It never
   looks at work, sensor data, contribution scores, sponsors, pods or TC
   balances, so a person with no pod and no earnings gets the same as an earner.
   Deposits need a unique receipt ID and a source label. Allocation happens once
   per day (repeat calls return a copy), shortages are reported, and integer
   remainders rotate between people by day.
2. **Distinct, active, bound attestors.** `MeshConsensus` now needs the
   `PersonhoodRegistry`. The claimer must be an active person bound to the
   claiming pod; only distinct, active, bound neighbor pods count. v8 counted
   `[P1, P1, P1]` as three attestations. Invalid peers are listed in
   `rejected` with a reason.
3. **Receipts.** Every claim names a unique `activity_id`. A met quorum returns
   a random `claim_ref` held by the mesh and bound to person, pod, domain,
   hours, polity and ledger hour.
4. **ContributionEngine** (v8 `AGIDataEngine`, kept as an alias) mints only
   against such a receipt from a mesh sharing its registry. It rejects missing,
   forged, mismatched, replayed or expired (>24 h) receipts; inactive or unbound
   claimants or attestors; unknown domains (v8 gave them weight 1.0); more than
   24 credited hours per person per workday; and a `day` that differs from the
   ledger clock. v8 never checked that quorum was met: its own demo minted at
   2 of 3 attestations.
5. **Integer-cent caps.** Per-person and global per-period caps are kept in
   integer cents, rounded down. The global cap is now mandatory; its default of
   60,000 TC per 30-day period is a **policy assumption**, not a calibrated
   result.
6. **provenance_score.** The provenance number is named `provenance_score`
   (`combined_entropy` remains as an alias); it was never entropy. Inputs are
   validated.
7. **Engine-owned variance band.** The engine scores sensor variance with its
   own plausible band and takes the attestation count from the stored receipt,
   so a claimant cannot widen the band or claim 999 attestations.

Review fixes beyond the GPT patch:

- A receipt is spent once it matches its activity, whatever the outcome. In
  the patch, a receipt rejected for low provenance could be retried with a
  better self-reported variance and then minted.
- Duplicate and inactive peers appear in `rejected` (the patch dropped them
  silently).
- `--test` still runs the v7/v8 built-in checks, updated to the receipt API,
  plus the v9 integration run (the patch replaced them with a smoke test).
- UN policy rejections are still written to the transparency log.
- Mesh logs are pruned past the longest window (v8 lists grew without limit).

## Tests

`test_substrate_v9.py`: **69 tests**, 67 pass and 2 skip by default (privacy
scan without `SUBSTRATE_PRIVACY_TERMS`, and the patch check if `v9.patch` or
`patch` is missing; both pass when enabled).

- All 36 v8 tests, ported to the receipt API with their intent kept. The six
  that failed on the patch (`TestMesh`: caller hour vs rate limit, rolling
  window, self-declared/one-sided neighbors, collusion caps; `TestBoundedBalance`:
  balance bound under max activity, global cap) failed only because the API
  changed (no registry or `activity_id`, no receipt). Ported, they pass both
  here and on the patched build, so they were not regressions.
- The patch's 28 tests, run on the real module (the patch's tests used a
  136-line stand-in of v8). The installer test became a "no runtime patching"
  test.
- 5 review tests: unbound person still gets basics, remainder rotation,
  rejected receipt cannot be retried, unknown domain rejected, invalid peers
  reported.

Optional privacy scan: `SUBSTRATE_PRIVACY_TERMS="term1,term2"`.

## Migrating callers

```python
mesh = MeshConsensus(registry=registry, clock=clock, graph=graph)
engine = ContributionEngine(mesh=mesh, registry=registry,
                            provenance_threshold=0.75,
                            global_cap_per_period=60000)
receipt = mesh.register(pod, "community_care", 1.0, peers,
                        activity_id="unique-real-activity-reference")
if receipt["quorum_met"]:
    activity.metadata["claim_ref"] = receipt["claim_ref"]
    credits, error = engine.evaluate_and_mint(activity)   # day from the clock
    if error is None:
        pod.credit(credits)
```

## Limitations (carried from the patch notes, still true)

- Basic-needs targets are caller-chosen policy numbers, not health,
  nutrition or engineering advice. Calorie sufficiency is not a complete diet.
- Deposits must be supplies actually available net of all production and
  infrastructure use. Shelter person-days are available capacity, not new
  housing. An allocation record is not proof that goods reached anyone.
- Equal-per-person is illustrative. A real system needs needs-based
  accommodations, nutrition quality, emergency priorities and independent
  appeals. Being in the registry is still an administrative gate, and the
  biometric registry is not an inclusive, production-ready eligibility system.
- Spoilage, procurement, grid dispatch, water safety and housing occupancy are
  not modeled. The pool is not wired to `PodModel`: annual averages cannot
  certify today's inventory.
- Relabeling one real activity under a new `activity_id` is fraud this code
  cannot detect. A ring of real, mutually linked neighbors can still collude up
  to the mesh caps.
- No global credit-supply invariant is claimed: sponsorship, direct
  `pod.credit` calls and exit settlements are separate paths.
- Neither `provenance_score` nor sensor variance proves human work, truth,
  social worth or a calibrated probability. No thermodynamic entropy is
  computed.
- This is not a security audit. Real deployments need authenticated
  approvals, real signatures, durable transactions, crash recovery, sensor
  audits and collusion controls. Python private attributes are not security
  barriers. Public logs can keep linkage after pseudonym erasure;
  crypto-shredding does not erase other parties' copies.
- Not fixed in v9: federation, sponsor accounting, exit-claim redemption,
  sanctions, the physical pod model and the calorie planner are as in v8.
  Basic-needs access must never depend on political sanctions or data sharing.
- Pod model sources and assumptions: see README_v8.md (unchanged).
  `pod_report_v9.json` is identical to `pod_report_v8.json`.

## Files

| File | What |
|---|---|
| substrate_v9.py | v9 source |
| test_substrate_v9.py | 69 regression tests |
| demo_output_v9.txt | `--demo` output |
| pod_report_v9.json | `--pod-report` output |
| v9.patch | v8 -> v9 diff, applies byte-exact |
