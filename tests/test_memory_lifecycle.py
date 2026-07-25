"""
Focused tests for memory lifecycle management (memory_lifecycle.py).

Synthetic memories only — no Chroma, no Joplin, no network, no real LLM
(proposer/judge are injected fakes). Covers tier assignment, decay,
reinforcement, duplicate + contradiction handling, judge rejection, archive /
restore, permanent-memory protection, bounded batch size, restart safety, and
dry-run producing no mutation.

Run:  venv/bin/python -m unittest tests.test_memory_lifecycle -v
"""

import asyncio
import os
import tempfile
import time
import unittest

os.environ["MEMORY_META_DB_PATH"] = tempfile.mktemp(suffix=".db")
import memory_lifecycle as ml

DAY = 86400.0


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Base(unittest.TestCase):
    def setUp(self):
        ml.DB_PATH = tempfile.mktemp(suffix=".db")
        ml._conn = None
        ml._access_buf.clear()
        ml._access_last.clear()
        ml._db()

    def mk(self, mid, segment="knowledge", **kw):
        return ml.upsert_meta(mid, segment=segment, **kw)


# ── tier assignment ─────────────────────────────────────────────────────────
class TierTests(Base):
    def test_kind_and_segment_mapping(self):
        self.assertEqual(ml.assign_defaults("fact")[:2], ("knowledge", "long"))
        self.assertEqual(ml.assign_defaults("identity")[:2], ("identity", "permanent"))
        self.assertEqual(ml.assign_defaults("conversation")[:2], ("context", "short"))
        self.assertEqual(ml.assign_defaults("preference")[:2], ("preference", "long"))

    def test_new_meta_inherits_tier(self):
        self.mk("m1", segment="identity")
        self.assertEqual(ml.get_meta("m1")["tier"], "permanent")
        self.mk("m2", segment="context")
        self.assertEqual(ml.get_meta("m2")["tier"], "short")


# ── decay + reinforcement ───────────────────────────────────────────────────
class DecayTests(Base):
    def test_short_decays_faster_than_long(self):
        now = time.time()
        s = self.mk("s", segment="context", tier="short", importance=1.0,
                    last_accessed_at=now - 10 * DAY, access_count=0)
        l = self.mk("l", segment="knowledge", tier="long", importance=1.0,
                    last_accessed_at=now - 10 * DAY, access_count=0)
        ss, ls = ml.effective_score(s, now), ml.effective_score(l, now)
        self.assertLess(ss, ls)
        self.assertLess(ss, 1.0)          # it decayed
        self.assertAlmostEqual(ss, 0.95 ** 10, places=3)

    def test_permanent_never_decays(self):
        now = time.time()
        p = self.mk("p", segment="identity", tier="permanent", importance=0.9,
                    last_accessed_at=now - 365 * DAY, access_count=0)
        self.assertAlmostEqual(ml.effective_score(p, now), 0.9, places=6)

    def test_reinforcement_raises_score(self):
        now = time.time()
        cold = self.mk("cold", tier="long", importance=0.5,
                       last_accessed_at=now, access_count=0)
        hot = self.mk("hot", tier="long", importance=0.5,
                      last_accessed_at=now, access_count=50)
        self.assertGreater(ml.effective_score(hot, now),
                           ml.effective_score(cold, now))


# ── batched retrieval accounting ────────────────────────────────────────────
class AccessTests(Base):
    def test_access_is_batched_then_flushed(self):
        self.mk("a", tier="long")
        ml.FLUSH_AT = 100
        for _ in range(5):
            ml.record_access(["a"])
        self.assertEqual(ml.get_meta("a")["access_count"], 0)   # buffered, not written
        n = ml.flush_access()
        self.assertEqual(n, 1)
        self.assertEqual(ml.get_meta("a")["access_count"], 5)


# ── write-time classification (extraction quality gate) ─────────────────────
class ClassifyTests(Base):
    def test_trivial_rejected(self):
        self.assertEqual(ml.classify_write("ok"), "trivial")

    def test_duplicate_detected(self):
        self.assertEqual(
            ml.classify_write("Boss keeps scooter tires at 45 psi",
                              ("scooter tires at 45 psi", 0.05)), "duplicate")

    def test_contradiction_flagged(self):
        # same subject, conflicting number, in the contradiction band
        label = ml.classify_write("Boss keeps scooter tires at 50 psi",
                                  ("Boss keeps scooter tires at 45 psi", 0.20))
        self.assertEqual(label, "contradiction")

    def test_novel_stored(self):
        self.assertEqual(ml.classify_write("Boss adopted a husky named Kaya",
                                           ("Boss drives a red scooter", 0.55)), "novel")


# ── archive / restore ───────────────────────────────────────────────────────
class ArchiveTests(Base):
    def test_archive_and_restore(self):
        self.mk("x", tier="long")
        self.assertTrue(ml.archive("x", reason="test"))
        self.assertEqual(ml.get_meta("x")["lifecycle"], "archived")
        self.assertIn("x", [r["memory_id"] for r in ml.archived_list()])
        self.assertTrue(ml.restore("x"))
        self.assertEqual(ml.get_meta("x")["lifecycle"], "active")

    def test_permanent_cannot_be_archived(self):
        self.mk("perm", segment="identity", tier="permanent")
        self.assertFalse(ml.archive("perm"))
        self.assertEqual(ml.get_meta("perm")["lifecycle"], "active")


# ── consolidation: proposals, judge, protection, bounds, dry-run ────────────
def fake_proposer(props):
    async def _p(payload):
        return list(props)
    return _p


def fake_judge(approved_indices):
    async def _j(proposals, payload):
        return set(approved_indices)
    return _j


class ConsolidateTests(Base):
    def seed(self, n, tier="long"):
        for i in range(n):
            self.mk(f"m{i}", tier=tier, importance=0.5,
                    last_accessed_at=time.time())
        return [dict(ml.get_meta(f"m{i}"), content=f"memory {i}") for i in range(n)]

    def test_duplicate_merge_proposal_applied(self):
        cands = self.seed(3)
        prop = [{"type": "merge", "keep": "m0", "absorb": ["m1"],
                 "rewrite": "combined"}]
        res = run(ml.consolidate(dry_run=False, candidates=cands,
                                 proposer=fake_proposer(prop),
                                 judge=fake_judge([0])))
        self.assertTrue(res["ok"])
        self.assertEqual(ml.get_meta("m1")["lifecycle"], "archived")
        self.assertIn("m1", eval(ml.get_meta("m0")["supersedes"]))
        self.assertEqual(res["applied"], 1)

    def test_contradiction_supersede_proposal_applied(self):
        cands = self.seed(2)
        prop = [{"type": "supersede", "newer": "m0", "older": ["m1"]}]
        res = run(ml.consolidate(dry_run=False, candidates=cands,
                                 proposer=fake_proposer(prop),
                                 judge=fake_judge([0])))
        self.assertEqual(ml.get_meta("m1")["lifecycle"], "archived")
        self.assertIn("m1", eval(ml.get_meta("m0")["supersedes"]))

    def test_judge_rejection_blocks_apply(self):
        cands = self.seed(2)
        prop = [{"type": "archive", "memory_id": "m1", "reason": "stale"}]
        res = run(ml.consolidate(dry_run=False, candidates=cands,
                                 proposer=fake_proposer(prop),
                                 judge=fake_judge([])))     # judge approves nothing
        self.assertEqual(res["applied"], 0)
        self.assertEqual(ml.get_meta("m1")["lifecycle"], "active")

    def test_permanent_protected_in_apply(self):
        cands = self.seed(1)
        self.mk("permx", segment="identity", tier="permanent")
        cands.append(dict(ml.get_meta("permx"), content="identity fact"))
        prop = [{"type": "archive", "memory_id": "permx", "reason": "x"}]
        res = run(ml.consolidate(dry_run=False, candidates=cands,
                                 proposer=fake_proposer(prop),
                                 judge=fake_judge([0])))
        self.assertEqual(ml.get_meta("permx")["lifecycle"], "active")
        self.assertEqual(res["protected_skipped"], 1)

    def test_bounded_batch_size(self):
        cands = self.seed(10)
        res = run(ml.consolidate(dry_run=True, candidates=cands, batch_size=3,
                                 proposer=fake_proposer([]),
                                 judge=fake_judge([])))
        self.assertEqual(res["scanned"], 3)

    def test_dry_run_no_mutation(self):
        cands = self.seed(2)
        prop = [{"type": "archive", "memory_id": "m1", "reason": "stale"}]
        res = run(ml.consolidate(dry_run=True, candidates=cands,
                                 proposer=fake_proposer(prop),
                                 judge=fake_judge([0])))
        self.assertEqual(res["applied"], 0)                       # nothing applied
        self.assertEqual(ml.get_meta("m1")["lifecycle"], "active")  # untouched
        self.assertEqual(res["report"]["applied"][0]["would_affect"], ["m1"])


# ── restart safety: persistence, single-run lock, stale reclaim, audit ──────
class RestartTests(Base):
    def test_meta_persists_across_reconnect(self):
        self.mk("keep", tier="long", importance=0.7, access_count=3)
        ml._conn = None                                   # simulate restart
        m = ml.get_meta("keep")
        self.assertIsNotNone(m)
        self.assertEqual(m["access_count"], 3)

    def test_no_concurrent_runs(self):
        now = time.time()
        ml._db().execute(
            "INSERT INTO consolidation_runs (run_id, trigger, dry_run, status, started_at) "
            "VALUES ('busy','manual',1,'running',?)", (now,))
        ml._db().commit()
        res = run(ml.consolidate(dry_run=True, candidates=[],
                                 proposer=fake_proposer([]), judge=fake_judge([])))
        self.assertFalse(res["ok"])
        self.assertIn("in progress", res["error"])

    def test_stale_lock_reclaimed(self):
        old = time.time() - (ml.RUN_STALE_SECS + 60)
        ml._db().execute(
            "INSERT INTO consolidation_runs (run_id, trigger, dry_run, status, started_at) "
            "VALUES ('stale','manual',1,'running',?)", (old,))
        ml._db().commit()
        res = run(ml.consolidate(dry_run=True, candidates=[],
                                 proposer=fake_proposer([]), judge=fake_judge([])))
        self.assertTrue(res["ok"])                        # proceeded past stale lock

    def test_audit_record_written(self):
        cands = [dict(self.mk("m0", tier="long"), content="c")]
        res = run(ml.consolidate(dry_run=True, candidates=cands,
                                 proposer=fake_proposer([]), judge=fake_judge([])))
        row = ml._db().execute("SELECT * FROM consolidation_runs WHERE run_id=?",
                               (res["run_id"],)).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["dry_run"], 1)


# ── migration: backup + idempotent add ──────────────────────────────────────
class MigrateTests(Base):
    def test_migrate_backs_up_and_adds(self):
        existing = [{"id": "n1", "kind": "fact"}, {"id": "n2", "kind": "identity"}]
        res = ml.migrate(existing)
        self.assertEqual(res["added"], 2)
        self.assertEqual(ml.get_meta("n2")["tier"], "permanent")
        # idempotent second run adds nothing
        self.assertEqual(ml.migrate(existing)["added"], 0)


if __name__ == "__main__":
    unittest.main()
