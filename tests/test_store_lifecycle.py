from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parent))
from store_lifecycle_checks import deletion_set


class StoreCollectionScopeTests(unittest.TestCase):
    def setUp(self):
        self.base = "/nix/store/" + "a" * 32 + "-protected"
        self.input = "/nix/store/" + "b" * 32 + "-fixture"
        self.referrer = "/nix/store/" + "c" * 32 + "-fixture.drv"

    def test_collection_is_limited_to_new_explicit_referrers(self):
        result = deletion_set([self.base], [self.base, self.input, self.referrer],
                              [self.input, self.referrer], self.input)
        self.assertEqual(set(result), {self.input, self.referrer})

    def test_protected_or_unknown_referrers_prevent_collection(self):
        current = [self.base, self.input, self.referrer]
        for referrers in [[self.input, self.base], [self.input, self.referrer + "-unknown"], [self.referrer]]:
            with self.subTest(referrers=referrers), self.assertRaisesRegex(RuntimeError, "preexisting or unknown"):
                deletion_set([self.base], current, referrers, self.input)

    def test_a_changed_base_inventory_prevents_collection(self):
        with self.assertRaisesRegex(RuntimeError, "protected store inventory changed"):
            deletion_set([self.base], [self.input], [self.input], self.input)

    def test_an_external_path_cannot_be_selected(self):
        outside = "/tmp/" + "b" * 32 + "-fixture"
        with self.assertRaisesRegex(RuntimeError, "outside the logical store"):
            deletion_set([self.base], [self.base, outside], [outside], outside)


if __name__ == "__main__":
    unittest.main()
