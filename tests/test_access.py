import unittest

from app.access import chunk_visible, make_identity


def payload(roles, team="*"):
    return {"allowed_roles": roles, "team_manager": team}


class Access(unittest.TestCase):
    def test_role_must_be_listed(self):
        p = payload(["sre_ops"])
        self.assertTrue(chunk_visible(p, make_identity("sre_ops")))
        for r in ("employee", "line_manager", "hr_ld"):
            self.assertFalse(chunk_visible(p, make_identity(r)))

    def test_line_manager_team_scope(self):
        p = payload(["hr_ld", "line_manager"], team="Vikram Singh")
        self.assertTrue(chunk_visible(p, make_identity("line_manager", "Vikram Singh")))
        self.assertFalse(chunk_visible(p, make_identity("line_manager", "Sara Thomas")))
        self.assertFalse(chunk_visible(p, make_identity("line_manager")))
        self.assertTrue(chunk_visible(p, make_identity("hr_ld")))

    def test_unscoped_rows_visible_to_line_manager(self):
        self.assertTrue(chunk_visible(payload(["line_manager"]), make_identity("line_manager", "Anyone")))

    def test_unknown_role(self):
        with self.assertRaises(ValueError):
            make_identity("admin")

    def test_cache_scope(self):
        self.assertEqual(make_identity("employee", "x").cache_scope, "employee")
        self.assertNotEqual(make_identity("line_manager", "A").cache_scope, make_identity("line_manager", "B").cache_scope)


if __name__ == "__main__":
    unittest.main()
