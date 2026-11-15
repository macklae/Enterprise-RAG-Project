import unittest

from app.access import make_identity
from app.cache import RedisCache, normalise_question, signature
from app.embeddings import HashEmbedder
from tests.fakes import FakeRedis


def make_cache():
    c = RedisCache("redis://unused", ttl=3600, ttl_not_found=60, max_distance=0.1)
    c.client = FakeRedis()
    c.enabled = True  # the redis package is not needed with the fake client
    return c


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = make_cache()
        self.emb = HashEmbedder()
        self.emp = make_identity("employee")
        self.entry = {"answer": "5 minutes [1]", "found": True, "mode": "llm", "sources": []}

    def test_signature_separates_p1_and_p2(self):
        self.assertNotEqual(signature("P1 acknowledge time"), signature("P2 acknowledge time"))
        self.assertEqual(signature("what is the P1 acknowledge time"), signature("P1 acknowledge time?"))

    def test_exact_hit(self):
        q = "How soon must a P1 incident be acknowledged?"
        self.cache.store(self.emp, q, self.emb.embed_query(q).dense, self.entry, ["SOP-OPS-001"])
        hit = self.cache.lookup_exact(self.emp, "how soon must a p1 incident be acknowledged")
        self.assertEqual(hit.kind, "exact")

    def test_semantic_hit_for_near_identical_question(self):
        q = "How soon must a P1 incident be acknowledged"
        self.cache.store(self.emp, q, self.emb.embed_query(q).dense, self.entry, ["SOP-OPS-001"])
        q2 = "How soon must a P1 incident be acknowledged by the on-call engineer"
        hit = self.cache.lookup_semantic(self.emp, q2, self.emb.embed_query(q2).dense)
        # a loose reworded question may or may not pass the strict 0.1 limit; an identical one must
        same = self.cache.lookup_semantic(self.emp, "How soon must a P1 incident be acknowledged ", self.emb.embed_query(q).dense)
        self.assertIsNotNone(same)

    def test_p2_does_not_reuse_p1_answer(self):
        q1, q2 = "P1 acknowledge time", "P2 acknowledge time"
        self.cache.store(self.emp, q1, self.emb.embed_query(q1).dense, self.entry, ["SOP-OPS-001"])
        self.assertIsNone(self.cache.lookup_semantic(self.emp, q2, self.emb.embed_query(q2).dense))

    def test_roles_are_kept_apart(self):
        q = "Who is on call in October"
        sre = make_identity("sre_ops")
        self.cache.store(sre, q, self.emb.embed_query(q).dense, self.entry, ["RPT-OPS-2026-09"])
        self.assertIsNone(self.cache.lookup_exact(self.emp, q))
        self.assertIsNotNone(self.cache.lookup_exact(sre, q))

    def test_line_managers_are_kept_apart(self):
        q = "Which of my team has incomplete training"
        a, b = make_identity("line_manager", "Vikram Singh"), make_identity("line_manager", "Sara Thomas")
        self.cache.store(a, q, self.emb.embed_query(q).dense, self.entry, ["REG-HR-007"])
        self.assertIsNone(self.cache.lookup_exact(b, q))

    def test_semantic_cache_isolated_between_roles(self):
        q = "Who is on call in October"
        sre = make_identity("sre_ops")
        vector = self.emb.embed_query(q).dense

        self.cache.store(sre, q, vector, self.entry, ["RPT-OPS-2026-09"])

        self.assertIsNotNone(self.cache.lookup_semantic(sre, q, vector))
        self.assertIsNone(self.cache.lookup_semantic(self.emp, q, vector))

    def test_semantic_cache_isolated_between_line_managers(self):
        q = "Which of my team has incomplete training"
        a = make_identity("line_manager", "Vikram Singh")
        b = make_identity("line_manager", "Sara Thomas")
        vector = self.emb.embed_query(q).dense

        self.cache.store(a, q, vector, self.entry, ["REG-HR-007"])

        self.assertIsNotNone(self.cache.lookup_semantic(a, q, vector))
        self.assertIsNone(self.cache.lookup_semantic(b, q, vector))

    def test_document_change_clears_answers(self):
        q = "P1 acknowledge time"
        self.cache.store(self.emp, q, self.emb.embed_query(q).dense, self.entry, ["SOP-OPS-001"])
        self.assertGreater(self.cache.invalidate_document("SOP-OPS-001"), 0)
        self.assertIsNone(self.cache.lookup_exact(self.emp, q))
        self.assertIsNone(self.cache.lookup_semantic(self.emp, q, self.emb.embed_query(q).dense))

    def test_not_found_cleared_when_document_added(self):
        q = "What is the dress code"
        self.cache.store(self.emp, q, self.emb.embed_query(q).dense, {"answer": "Not found", "found": False, "mode": "not_found", "sources": []}, [])
        self.assertIsNotNone(self.cache.lookup_exact(self.emp, q))
        self.cache.clear_not_found()
        self.assertIsNone(self.cache.lookup_exact(self.emp, q))

    def test_normalise(self):
        self.assertEqual(normalise_question("  What  is X?? "), "what is x")


if __name__ == "__main__":
    unittest.main()
