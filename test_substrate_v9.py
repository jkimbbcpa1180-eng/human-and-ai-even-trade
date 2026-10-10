# SPDX-License-Identifier: CC0-1.0
"""Regression tests for Substrate v9. Credit: humanity.
Run: python -m unittest test_substrate_v9 -v
Part 1: the 36 v8 tests, ported to the v9 receipt API with the same intent.
Part 2: v9 tests (from the review of the v9 patch), run on the real module.
Optional privacy scan: SUBSTRATE_PRIVACY_TERMS="term1,term2" (comma-separated,
case-insensitive) fails if any term appears in the publishable files."""
import hashlib
import os
import random
import shutil
import subprocess
import tempfile
import unittest

import importlib

s = importlib.import_module(os.environ.get("SUBSTRATE_MODULE", "substrate_v9"))

HERE = os.path.dirname(os.path.abspath(__file__))


def bound_registry(pods):
    """Registry whose people own `pods` (pod.person_id), one pod each."""
    r = s.PersonhoodRegistry()
    r.register_genesis([(p.person_id, f"bio-{p.person_id}") for p in pods])
    for p in pods:
        r.bind_pod(p.person_id, p.node_id)
    return r


def genesis_registry(**kw):
    r = s.PersonhoodRegistry(**kw)
    r.register_genesis([("g1", "b1"), ("g2", "b2"), ("g3", "b3")])
    return r


class TestPersonhood(unittest.TestCase):
    def test_duplicate_vouch_counts_once(self):
        r = genesis_registry()
        out = r.register("x", "bx", sponsors=["g1", "g1"])
        self.assertEqual(out["status"], "FAILED")
        self.assertIn("have 1", out["reason"])

    def test_self_vouch_refused(self):
        r = genesis_registry()
        r.register("x", "bx", sponsors=["g1", "g2"])
        out = r.register("x2", "bx2", sponsors=["x2", "g1"])
        self.assertEqual(out["status"], "FAILED")

    def test_genesis_quorum_required(self):
        r = s.PersonhoodRegistry()
        self.assertEqual(r.register("a", "ba")["status"], "FAILED")
        self.assertEqual(r.register_genesis([("a", "1"), ("b", "2")])["status"], "FAILED")
        self.assertEqual(r.register_genesis([("a", "1"), ("b", "2"), ("c", "2")])["status"],
                         "FAILED")  # duplicate biometric
        self.assertEqual(r.register_genesis([("a", "1"), ("b", "2"), ("c", "3")])["status"],
                         "GENESIS")
        self.assertEqual(r.persons["a"].sponsors, {"b", "c"})

    def test_setup_bootstraps_honestly(self):
        _, reg, pods = s._setup()
        for pid, att in reg.persons.items():
            self.assertNotIn(pid, att.sponsors)
            self.assertGreaterEqual(len(att.sponsors), 2)
        self.assertEqual(len(pods), 5)

    def test_biometric_pepper_per_registry(self):
        a, b = s.PersonhoodRegistry(), s.PersonhoodRegistry()
        self.assertNotEqual(a._hash("same"), b._hash("same"))
        self.assertNotEqual(a._hash("same"),
                            hashlib.sha256(b"autarky-v7|same").hexdigest()[:24])


class TestMesh(unittest.TestCase):
    def setUp(self):
        self.pods = [s.HouseholdPod(f"P{i}", person_id=f"x{i}") for i in range(6)]
        self.g = s.NeighborGraph()
        for p in self.pods:
            self.g.register_pod(p.node_id)
        for n in ("P1", "P2", "P3"):
            self.g.link("P0", n)
        self.clock = s.LedgerClock()
        self.reg = bound_registry(self.pods)
        self.n = 0

    def mesh(self, **kw):
        return s.MeshConsensus(clock=self.clock, graph=self.g, registry=self.reg, **kw)

    def claim(self, m, peers, **kw):
        self.n += 1
        return m.register(self.pods[0], "craft", 1, peers, activity_id=f"a{self.n}", **kw)

    def test_caller_hour_cannot_bypass_rate_limit(self):
        m = self.mesh(quorum=1, rate_limit_per_hour=2,
                      pair_cap_per_period=99, set_cap_per_period=99)
        peer = [self.pods[1]]
        got = [self.claim(m, peer, hour=h)["accepted"]
               for h in range(10)]  # v7: each new 'hour' reset the limit
        self.assertEqual(sum(1 for a in got if a), 2)

    def test_rolling_window(self):
        m = self.mesh(quorum=1, rate_limit_per_hour=2, window_hours=3,
                      pair_cap_per_period=99, set_cap_per_period=99)
        peer = [self.pods[1]]
        self.claim(m, peer)
        self.clock.advance(1)
        self.claim(m, peer)
        self.clock.advance(1)
        self.assertEqual(self.claim(m, peer)["accepted"], [])
        self.clock.advance(1)
        self.assertEqual(self.claim(m, peer)["accepted"], ["P1"])

    def test_clock_monotonic(self):
        with self.assertRaises(ValueError):
            self.clock.advance(-1)

    def test_self_declared_and_one_sided_neighbors_rejected(self):
        self.pods[0].neighbors = {"P4", "P5"}   # self-declared: ignored
        self.g.propose("P0", "P4")               # one-sided: not an edge
        m = self.mesh(quorum=1)
        r = self.claim(m, [self.pods[4], self.pods[5]])
        self.assertEqual(r["accepted"], [])
        self.assertNotIn("claim_ref", r)
        self.assertEqual(self.g.propose("P0", "UNREG")["status"], "FAILED")

    def test_collusion_caps(self):
        m = self.mesh(quorum=3, rate_limit_per_hour=99,
                      pair_cap_per_period=99, set_cap_per_period=3)
        ring = self.pods[1:4]
        oks = []
        for _ in range(6):
            self.clock.advance(1)
            oks.append(self.claim(m, ring)["quorum_met"])
        self.assertEqual(sum(oks), 3)
        self.clock.advance(24)
        self.assertTrue(self.claim(m, ring)["quorum_met"])
        m2 = self.mesh(quorum=1, rate_limit_per_hour=99,
                       pair_cap_per_period=2, set_cap_per_period=99)
        n = sum(bool(self.claim(m2, [self.pods[1]])["accepted"])
                for _ in range(5))
        self.assertEqual(n, 2)


def mint_world(n_people, **engine_kw):
    """n people with bound pods, all mutually linked, generous mesh caps
    (these tests target the ENGINE caps, not the mesh caps)."""
    pods = [s.HouseholdPod(f"P{i}", person_id=f"x{i}") for i in range(n_people)]
    g, clock = s.NeighborGraph(), s.LedgerClock()
    for p in pods:
        g.register_pod(p.node_id)
    for i, a in enumerate(pods):
        for b in pods[i + 1:]:
            g.link(a.node_id, b.node_id)
    reg = bound_registry(pods)
    mesh = s.MeshConsensus(clock=clock, graph=g, registry=reg, rate_limit_per_hour=99,
                           pair_cap_per_period=10**6, set_cap_per_period=10**6)
    eng = s.ContributionEngine(mesh=mesh, registry=reg, **engine_kw)
    count = [0]

    def attempt(pod, domain, hours, variance=0.3):
        count[0] += 1
        peers = [q for q in pods if q is not pod][:3]
        att = mesh.register(pod, domain, hours, peers, activity_id=f"act-{count[0]}")
        act = s.MammalianActivity(domain, "x", hours, s.ProvenanceEvidence(variance, 3),
                                  1.0, pod_id=pod.node_id, person_id=pod.person_id,
                                  metadata={"claim_ref": att.get("claim_ref")})
        return eng.evaluate_and_mint(act)
    return pods, clock, eng, attempt


class TestBoundedBalance(unittest.TestCase):
    def test_balance_bounded_under_max_activity(self):
        # v9: each attempt needs its own receipt and at most 24 credited
        # hours per workday, so max activity is one 24 h claim per day.
        # One 24 h claim asks 864 TC; 4 a month ask 3,456 against a 600 cap.
        pods, clock, agi, attempt = mint_world(4, person_cap_per_period=600.0)
        pod = pods[0]
        bound = pod.balance_bound(600.0)
        peak = 0.0
        for month in range(120):
            for k in range(4):
                clock.advance(month * 30 * 24 + k * 24 - clock.hour)
                c, _ = attempt(pod, "community_care", 24.0)
                pod.credit(c)
            minted = agi.minted_in_period("x0", month * 30)
            self.assertLessEqual(minted, 600.0 + 1e-6)
            self.assertAlmostEqual(minted, 600.0)       # cap is reached, not dodged
            pod.step_month(is_active=True)
            peak = max(peak, pod.transit_credit_balance)
        self.assertLessEqual(peak, bound + 0.01)
        self.assertGreater(peak, 0.95 * bound)  # bound is tight, not vacuous

    def test_global_cap(self):
        pods, clock, agi, attempt = mint_world(10, person_cap_per_period=1e9,
                                               global_cap_per_period=100.0)
        tot = sum(attempt(p, "craft", 10.0)[0] for p in pods)
        self.assertAlmostEqual(tot, 100.0, places=2)


class TestExit(unittest.TestCase):
    def test_exit_settles_unbinds_keeps_personhood(self):
        reg = genesis_registry()
        pod = s.HouseholdPod("POD", transit_credit_balance=321.0, person_id="g1")
        reg.bind_pod("g1", "POD")
        pol = s.Polity("Poly")
        pol.admit(pod)
        log = s.TransparencyLog()
        r = pol.exit(pod, reg, log)
        self.assertEqual(r["status"], "EXITED")
        self.assertEqual(r["settled_tc"], 321.0)
        self.assertEqual(pod.transit_credit_balance, 0.0)
        self.assertEqual(reg.pod_count(), 0)
        self.assertIn("g1", reg.persons)
        self.assertNotIn("POD", pol.member_pods)
        self.assertEqual(pol.exit(pod, reg)["status"], "FAILED")

    def test_exit_with_erasure(self):
        reg = genesis_registry()
        pod = s.HouseholdPod("POD", transit_credit_balance=10.0, person_id="g2")
        reg.bind_pod("g2", "POD")
        pol = s.Polity("Poly")
        pol.admit(pod)
        log = s.TransparencyLog()
        log.append("mint", {"pod_id": "POD", "domain": "craft", "hours": 2}, 1)
        ps = log.pseudonym("POD")
        r = pol.exit(pod, reg, log, delete_personhood=True)
        self.assertNotIn("g2", reg.persons)
        self.assertGreaterEqual(r["log_entries_erased"], 2)
        dump = repr(log.events)
        self.assertNotIn(ps, dump)
        self.assertNotIn("POD'", dump)
        self.assertEqual(pol.exit_settlements[-1]["person_id"], "erased")


class TestUN(unittest.TestCase):
    def test_severity_single_source(self):
        lad = s.UNEscalationLadder()
        for p in s.STANDARD_PROHIBITIONS:
            self.assertIn(p.code, lad.MAJOR_CODES if p.severity == "major"
                          else lad.MINOR_CODES)
        self.assertIn("UNP-01", lad.MAJOR_CODES)
        self.assertNotIn("UNP-01", lad.MINOR_CODES)
        un = s.UNRecognition(["KR"])
        un.ladder.record_violation("X", "UNP-01", 1)
        self.assertEqual(un.ladder.current_stage("X")[0], 2)  # major -> stage 2

    def test_appeal_requires_independent_quorum(self):
        lad = s.UNEscalationLadder(review_quorum=3)
        lad.record_violation("X", "UNP-01", 1)
        self.assertEqual(lad.appeal("X", "   ", 2)["status"], "FAILED")
        a = lad.appeal("X", "any text", 2)
        self.assertEqual(a["status"], "PENDING")
        self.assertEqual(lad.current_stage("X")[0], 2)   # not auto-granted
        lad.register_reviewer("in", "X")
        self.assertEqual(lad.review(a["appeal_id"], "in", True)["status"], "FAILED")
        self.assertEqual(lad.review(a["appeal_id"], "nobody", True)["status"], "FAILED")
        for i in range(3):
            lad.register_reviewer(f"r{i}", f"Y{i}")
        lad.review(a["appeal_id"], "r0", True)
        lad.review(a["appeal_id"], "r0", True)           # duplicate vote counts once
        self.assertEqual(lad.current_stage("X")[0], 2)
        lad.review(a["appeal_id"], "r1", True)
        out = lad.review(a["appeal_id"], "r2", True)
        self.assertEqual(out["status"], "GRANTED")
        self.assertEqual(lad.current_stage("X")[0], 0)
        lad.record_violation("X", "UNP-03", 3)           # recompute must not undo
        self.assertEqual(lad.current_stage("X")[0], 1)

    def test_appeal_denied(self):
        lad = s.UNEscalationLadder(review_quorum=2)
        lad.record_violation("X", "UNP-02", 1)
        a = lad.appeal("X", "text", 2)
        for i in range(2):
            lad.register_reviewer(f"r{i}", "Z")
            lad.review(a["appeal_id"], f"r{i}", False)
        self.assertEqual(lad.appeals[0]["status"], "DENIED")
        self.assertEqual(lad.current_stage("X")[0], 2)


class TestAuditors(unittest.TestCase):
    def test_forged_certificate_rejected(self):
        reg = s.AuditorRegistry(seed=1)
        a = reg.register("A", "A", 1000.0)
        cert = s.ESGCertificate.issue("SP", "POD", "carbon", 10.0, 1, None, reg)
        self.assertTrue(reg.verify(cert))
        v7_sig = hashlib.sha256(f"{cert.certificate_id}|A|{a.bond_tc}".encode()).hexdigest()[:24]
        self.assertFalse(reg.verify(s.ESGCertificate(**{**cert.__dict__,
                                                       "auditor_signature": v7_sig})))
        self.assertFalse(reg.verify(s.ESGCertificate(**{**cert.__dict__, "amount": 99.0})))

    def test_sponsor_cannot_choose_auditor_and_conflicts_excluded(self):
        reg = s.AuditorRegistry(seed=3)
        a = reg.register("A", "A", 1000.0)
        b = reg.register("B", "B", 1000.0)
        reg.declare_conflict("A", "SP")
        for d in range(20):
            self.assertEqual(reg.assign("SP", "POD", "m", d).auditor_id, "B")
        self.assertIsNone(s.ESGCertificate.issue("SP", "POD", "m", 1.0, 0, a, reg))

    def test_slash_minimum_sample_rule(self):
        def make(n_certs):
            reg = s.AuditorRegistry(seed=0)
            a = reg.register("A", "A", 10_000.0)
            a.issued_certificates = [f"c{i}" for i in range(n_certs)]
            return reg, a
        reg, a = make(1)
        self.assertFalse(reg.slash("A", 1, "x")["removed"])     # v7 removed here
        reg, a = make(100)
        reg.slash("A", 1, "x"); r = reg.slash("A", 1, "x")
        self.assertFalse(r["removed"])                          # 2% of 100
        reg, a = make(100)
        r = None
        for _ in range(3):
            r = reg.slash("A", 1, "x")
        self.assertTrue(r["removed"])                           # absolute limit
        reg2 = s.AuditorRegistry()
        reg2.ABS_SLASH_LIMIT = 10**9
        a2 = reg2.register("A", "A", 10_000.0)
        a2.issued_certificates = [f"c{i}" for i in range(40)]
        k = 0
        while not a2.removed:
            reg2.slash("A", 1, "x")
            k += 1
        # first k with Wilson 95% lower bound of k/40 above 5%
        self.assertEqual(k, min(j for j in range(1, 41) if s.wilson_lower(j, 40) > 0.05))
        self.assertGreater(k, 1)
        self.assertAlmostEqual(s.wilson_lower(5, 40), 0.0546, places=3)


class TestFederationTreasury(unittest.TestCase):
    def test_no_overwrite(self):
        ra = genesis_registry()
        rb = s.PersonhoodRegistry()
        rb.register_genesis([("g1", "other-person"), ("h2", "x"), ("h3", "y")])
        before = rb.persons["g1"]
        r = s.Federation().recognize_personhood(ra, rb, "g1", biometric="b1")
        self.assertEqual(r["status"], "FAILED")
        self.assertIs(rb.persons["g1"], before)

    def test_fees_conserved(self):
        a, b = s.Polity("A"), s.Polity("B")
        pa, pb = s.HouseholdPod("PA", transit_credit_balance=100.0), s.HouseholdPod("PB")
        a.admit(pa); b.admit(pb)
        fed = s.Federation()
        fed.sign("A", "B", cross_polity_fee_pct=0.02)
        fed.cross_polity_transfer(pa, pb, 40.0, a, b)
        total = pa.transit_credit_balance + pb.transit_credit_balance + fed.fee_treasury.balance
        self.assertAlmostEqual(total, 100.0)

    def test_treasury_needs_distinct_registered_members(self):
        reg = genesis_registry()
        reg.register("m4", "b4", sponsors=["g1", "g2"])
        t = s.Treasury(registry=reg, min_approvals=3)
        t.deposit(100.0, "x")
        self.assertEqual(t.spend(10, "p", {"g1", "g1", "ghost", "outsider"})["status"], "FAILED")
        self.assertEqual(t.spend(10, "p", {"g1", "g2", "g3"})["status"], "OK")
        reg.persons["g3"].exited = True
        self.assertEqual(t.spend(10, "p", {"g1", "g2", "g3"})["status"], "FAILED")


class TestPrivacyAndProvenance(unittest.TestCase):
    def test_log_pseudonymous_and_aggregated(self):
        log = s.TransparencyLog(privacy=True, k_anon=3)
        for i in range(2):
            log.append("mint", {"pod_id": f"POD-{i}", "domain": "care", "hours": 5.0}, 1)
        dump = repr(log.events)
        self.assertNotIn("POD-0", dump)
        self.assertNotIn("hours", dump)
        self.assertIsNone(log.aggregate_report()[0]["hours"])   # < k suppressed
        log.append("mint", {"pod_id": "POD-2", "domain": "care", "hours": 5.0}, 1)
        self.assertEqual(log.aggregate_report()[0]["hours"], 15.0)
        self.assertEqual(log.pseudonym("POD-0"), log.pseudonym("POD-0"))

    def test_noisy_sensors_do_not_mint_more(self):
        base = s.ProvenanceEvidence(0.3, 3).provenance_score
        prev = base
        for v in (0.7, 0.9, 1.0, 2.0):
            e = s.ProvenanceEvidence(v, 3).provenance_score
            self.assertLessEqual(e, prev)
            prev = e
        self.assertLess(s.ProvenanceEvidence(0.0, 3).provenance_score, base)

    def test_privacy_terms_absent(self):
        terms = [t.strip().lower() for t in
                 os.environ.get("SUBSTRATE_PRIVACY_TERMS", "").split(",") if t.strip()]
        if not terms:
            self.skipTest("SUBSTRATE_PRIVACY_TERMS not set")
        files = ["substrate_v9.py", "test_substrate_v9.py", "README_v9.md",
                 "demo_output_v9.txt", "pod_report_v9.json", "v9.patch"]
        for f in files:
            p = os.path.join(HERE, f)
            if not os.path.exists(p):
                continue
            text = open(p, encoding="utf-8").read().lower()
            for t in terms:
                self.assertNotIn(t, text, f"privacy term found in {f}")


class TestPodModel(unittest.TestCase):
    def test_energy_balance(self):
        m = s.PodModel()
        r = m.report()
        self.assertAlmostEqual(r["load_kwh_day"],
                               m.p.base_load_kwh_day + m.grow_kwh_day(), places=1)
        mol = m.p.grow_area_m2 * m.dli()
        self.assertAlmostEqual(m.grow_kwh_day(),
                               mol / 2.7e-6 / 3.6e6 * m.p.hvac_overhead, places=6)
        self.assertAlmostEqual(r["pv_kwh_day_worst_month"], 6 * 1.90, places=2)
        self.assertFalse(r["sustainable"])
        self.assertLess(r["calorie_share"], 0.10)

    def test_monotonic(self):
        def rep(**kw):
            return s.PodModel(s.PodParams(**kw)).report()
        a = [rep(grow_area_m2=x)["indoor_kcal_day"] for x in (1, 5, 10, 50)]
        self.assertEqual(a, sorted(a))
        e = [rep(led_efficacy_umol_j=x)["grow_kwh_day"] for x in (1.5, 2.5, 3.4, 4.1)]
        self.assertEqual(e, sorted(e, reverse=True))
        c = [rep(grow_area_m2=x)["annualized_cost_usd"] for x in (1, 10, 100)]
        self.assertEqual(c, sorted(c))

    def test_calories_never_exceed_light_bound(self):
        rng = random.Random(0)
        for _ in range(500):
            p = s.PodParams(grow_area_m2=rng.uniform(0, 200),
                            ppfd_umol_m2_s=rng.uniform(0, 1500),
                            photoperiod_h=rng.uniform(0, 24),
                            crop=rng.choice(list(s.CROP_KCAL_PER_MOL)))
            m = s.PodModel(p)
            self.assertLessEqual(m.indoor_kcal_day(), m.light_ceiling_kcal_day() + 1e-9)

    def test_sustainable_is_computed(self):
        p = s.PodParams(persons=1, pv_kwp=40, battery_kwh=40, roof_m2=200,
                        outdoor_area_m2=600)
        r = s.PodModel(p).report()
        self.assertTrue(r["sustainable"])
        pod = s.HouseholdPod("P")
        d = pod.cycle_day(10, 5, 50, 100)
        self.assertGreater(d["cost_usd"], 0)
        self.assertFalse(d["food_self_sufficient"])


class TestCaloriePlanner(unittest.TestCase):
    def test_known_bmr(self):
        self.assertAlmostEqual(s.mifflin_st_jeor(80, 175, 40, "male"), 1698.75)
        self.assertAlmostEqual(s.mifflin_st_jeor(60, 165, 30, "female"), 1320.25)

    def test_deficit_and_weeks(self):
        p = s.plan_calories(175, 80, 40, "male", "moderate", 72)
        self.assertAlmostEqual(p["tdee"], 1698.75 * 1.55, places=0)
        self.assertAlmostEqual(p["target_kcal_day"], p["tdee"] - 500, places=0)
        self.assertAlmostEqual(p["weeks_to_goal_est"], 8 * 7700 / 3500, places=1)
        g = s.plan_calories(175, 60, 25, "male", "light", 66)
        self.assertGreater(g["target_kcal_day"], g["tdee"])

    def test_floor_enforced(self):
        p = s.plan_calories(150, 55, 70, "female", "sedentary", 50)
        self.assertTrue(p["floor_applied"])
        self.assertEqual(p["target_kcal_day"], 1200.0)
        m = s.plan_calories(155, 60, 80, "male", "sedentary", 55)
        self.assertGreaterEqual(m["target_kcal_day"], 1500.0)

    def test_underweight_goal_refused(self):
        p = s.plan_calories(175, 70, 30, "female", "light", 55)   # BMI 18.0
        self.assertEqual(p["status"], "REFUSED")
        self.assertEqual(s.pod_supply_for_plan(p)["status"], "REFUSED")

    def test_feeds_pod(self):
        p = s.plan_calories(175, 80, 40, "male", "moderate", 72)
        out = s.pod_supply_for_plan(p)
        self.assertAlmostEqual(out["pod_share"],
                               out["pod_kcal_day"] / p["target_kcal_day"], places=3)


def find_source(name):
    """publish/ first (local layout), then next to this file (repo layout)."""
    for p in (os.path.join(HERE, "publish", name), os.path.join(HERE, name)):
        if os.path.exists(p):
            return p
    return None


class TestPatch(unittest.TestCase):
    def test_patch_applies_byte_exact(self):
        patch = os.path.join(HERE, "v9.patch")
        src8 = find_source("substrate_v8.py")
        if not os.path.exists(patch) or src8 is None or shutil.which("patch") is None:
            self.skipTest("no v9.patch, substrate_v8.py or patch tool")
        with tempfile.TemporaryDirectory() as d:
            dst = os.path.join(d, "out.py")
            shutil.copy(src8, dst)
            subprocess.run(["patch", "-s", dst, patch], check=True)
            with open(dst, "rb") as f1, open(os.path.join(HERE, "substrate_v9.py"), "rb") as f2:
                self.assertEqual(f1.read(), f2.read())


class TestV9(unittest.TestCase):
    """Intent of the 28 tests shipped with the v9 patch, on the real module
    (the patch's own tests ran on a 136-line stand-in of v8)."""
    def setUp(self):
        self.reg, self.clock, self.graph, pods = s._toy_world(4, polity="Home")
        self.pods = pods
        self.mesh = s.MeshConsensus(registry=self.reg, clock=self.clock, graph=self.graph)
        self.engine = s.ContributionEngine(mesh=self.mesh, registry=self.reg)
        self.targets = dict(food_kcal=2500, water_ml=20000, energy_wh=4000,
                            shelter_person_days=1)
        self.pool = s.BasicNeedsPool(self.reg, self.clock, self.targets)
        self.p0 = self.pods[0].person_id

    def claim(self, name="work-1", hours=1):
        att = self.mesh.register(self.pods[0], "community_care", hours, self.pods[1:],
                                 activity_id=name)
        return s.MammalianActivity("community_care", "test", hours,
                                   s.ProvenanceEvidence(.3, 999), 1., "pod-0", self.p0,
                                   "Home", {"claim_ref": att["claim_ref"]})

    def test_honest_award(self):
        self.assertEqual(self.engine.evaluate_and_mint(self.claim()), (36., None))

    def test_duplicate_peers(self):
        a = self.mesh.register(self.pods[0], "community_care", 1, [self.pods[1]] * 3,
                               activity_id="a")
        self.assertFalse(a["quorum_met"])
        self.assertNotIn("claim_ref", a)

    def test_two_peers(self):
        a = self.mesh.register(self.pods[0], "community_care", 1, self.pods[1:3],
                               activity_id="a")
        self.assertFalse(a["quorum_met"])

    def test_no_trusted_mesh(self):
        self.assertEqual(s.ContributionEngine().evaluate_and_mint(self.claim())[0], 0)
        other = s.MeshConsensus(registry=s.PersonhoodRegistry(), clock=self.clock,
                                graph=self.graph)
        e = s.ContributionEngine(mesh=other, registry=self.reg)
        self.assertEqual(e.evaluate_and_mint(self.claim("w2"))[0], 0)

    def test_forged_receipt(self):
        a = self.claim()
        a.metadata["claim_ref"] = "made-up"
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)

    def test_replay(self):
        a = self.claim()
        self.engine.evaluate_and_mint(a)
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)

    def test_duplicate_activity(self):
        self.claim()
        with self.assertRaises(ValueError):
            self.claim()

    def test_tampered_hours(self):
        a = self.claim()
        a.hours_logged = 12
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)

    def test_tampered_person(self):
        a = self.claim()
        a.person_id = self.pods[1].person_id
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)

    def test_expired(self):
        a = self.claim()
        self.clock.advance(25)
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)

    def test_clock_spoof(self):
        self.assertEqual(self.engine.evaluate_and_mint(self.claim(), day=30)[0], 0)

    def test_exited(self):
        a = self.claim()
        self.reg.persons[self.p0].exited = True
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)

    def test_attestor_exited(self):
        a = self.claim()
        self.reg.persons[self.pods[1].person_id].exited = True
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)

    def test_caps(self):
        e = s.ContributionEngine(mesh=self.mesh, registry=self.reg,
                                 person_cap_per_period=20.009, global_cap_per_period=10.009)
        self.assertEqual(e.evaluate_and_mint(self.claim())[0], 10.)
        self.clock.advance(1)
        self.assertEqual(e.evaluate_and_mint(self.claim("work-2"))[0], 0)
        with self.assertRaises(ValueError):
            s.ContributionEngine(global_cap_per_period=None)

    def test_nonfinite(self):
        for v in (float("nan"), float("inf"), -1, 25):
            with self.subTest(v=v), self.assertRaises(ValueError):
                self.mesh.register(self.pods[0], "community_care", v, self.pods[1:],
                                   activity_id="a")

    def test_rating_range(self):
        a = self.claim()
        a.peer_consensus_rating = 2
        with self.assertRaises(ValueError):
            self.engine.evaluate_and_mint(a)

    def test_claimant_must_be_bound(self):
        self.reg.unbind_pod(self.p0)
        with self.assertRaises(ValueError):
            self.claim()

    def test_daily_hours(self):
        self.assertGreater(self.engine.evaluate_and_mint(self.claim(hours=13))[0], 0)
        self.clock.advance(1)
        self.assertEqual(self.engine.evaluate_and_mint(self.claim("work-2", 13))[0], 0)

    def test_unconditional_basics(self):
        self.pods[0].credit(500.0)      # one person has earned credits
        self.pool.deposit("delivery", {k: v * 4 for k, v in self.targets.items()},
                          "demo inventory, not observed")
        r = self.pool.allocate_today()
        self.assertEqual(r["status"], "ALLOCATED")
        for p in self.pods:
            self.assertEqual(r["allocations"][p.person_id], self.targets)
        self.assertTrue(all(v == 0 for v in self.pool.inventory.values()))

    def test_unbound_person_still_gets_basics(self):
        self.reg.unbind_pod(self.pods[3].person_id)     # no pod, no work
        self.pool.deposit("d", {k: v * 4 for k, v in self.targets.items()}, "test")
        r = self.pool.allocate_today()
        self.assertEqual(r["allocations"][self.pods[3].person_id], self.targets)

    def test_shortage(self):
        self.pool.deposit("d", dict.fromkeys(self.targets, 3), "test")
        r = self.pool.allocate_today()
        self.assertEqual(r["status"], "SHORTFALL")
        for k in self.targets:
            self.assertEqual(sum(v[k] for v in r["allocations"].values()), 3)
            self.assertEqual(r["shortfall"][k], self.targets[k] * 4 - 3)

    def test_remainder_rotates(self):
        got = []
        for day in range(4):
            self.pool.deposit(f"d{day}", dict.fromkeys(self.targets, 1), "test")
            r = self.pool.allocate_today()
            got.append(next(p for p, a in r["allocations"].items() if a["food_kcal"]))
            self.clock.advance(24)
        self.assertEqual(len(set(got)), 4)

    def test_basics_idempotent_and_copied(self):
        r = self.pool.allocate_today()
        r["allocations"].clear()
        self.assertEqual(len(self.pool.allocate_today()["allocations"]), 4)

    def test_duplicate_supply(self):
        self.pool.deposit("d", self.targets, "test")
        with self.assertRaises(ValueError):
            self.pool.deposit("d", self.targets, "test")
        with self.assertRaises(ValueError):
            self.pool.deposit("e", self.targets, "  ")

    def test_supply_validation(self):
        for value in (-1, 1.5, float("nan"), True):
            with self.subTest(v=value), self.assertRaises(ValueError):
                self.pool.deposit("d", {**self.targets, "food_kcal": value}, "test")

    def test_new_day_allocates_again(self):
        self.pool.allocate_today()
        self.clock.advance(24)
        self.pool.deposit("d", {k: v * 4 for k, v in self.targets.items()}, "test")
        self.assertEqual(self.pool.allocate_today()["status"], "ALLOCATED")

    def test_provenance_alias(self):
        p = s.ProvenanceEvidence(.3, 3)
        self.assertEqual(p.provenance_score, 1)
        self.assertEqual(p.combined_entropy, p.provenance_score)
        self.assertIs(s.AGIDataEngine, s.ContributionEngine)

    def test_no_runtime_patching(self):
        # replaces the patch's installer test: v9 is plain source
        with open(os.path.join(HERE, "substrate_v9.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("exec(", text)
        self.assertNotIn("SUBSTRATE_PATCH_VERSION", text)
        self.assertEqual(s.SUBSTRATE_VERSION, "9.0")

    def test_exercise(self):
        out = s.basic_needs_exercise()
        self.assertEqual(out["earned_credits"], 36)
        self.assertEqual(out["nonworker_credits"], 0)

    def test_claimant_cannot_choose_variance_band(self):
        a = self.claim()
        a.provenance.sensor_variance = 100
        a.provenance.plausible_high = 200
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)

    def test_rejected_receipt_cannot_be_retried(self):
        # review addition: after a quality rejection the receipt is spent,
        # so tweaking the self-reported variance and retrying earns nothing
        a = self.claim()
        a.provenance.sensor_variance = 5.0
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)
        a.provenance.sensor_variance = 0.3
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)

    def test_unknown_domain_rejected(self):
        att = self.mesh.register(self.pods[0], "made_up", 1, self.pods[1:], activity_id="u")
        a = s.MammalianActivity("made_up", "x", 1, s.ProvenanceEvidence(.3, 3), 1.,
                                "pod-0", self.p0, "Home", {"claim_ref": att["claim_ref"]})
        self.assertEqual(self.engine.evaluate_and_mint(a)[0], 0)

    def test_invalid_peers_are_reported(self):
        self.reg.persons[self.pods[2].person_id].exited = True
        a = self.mesh.register(self.pods[0], "craft", 1,
                               [self.pods[1], self.pods[1], self.pods[2]], activity_id="r")
        reasons = dict((p, why) for p, why in a["rejected"])
        self.assertEqual(reasons, {"pod-1": "duplicate peer",
                                   "pod-2": "inactive or unbound pod"})


if __name__ == "__main__":
    unittest.main()
