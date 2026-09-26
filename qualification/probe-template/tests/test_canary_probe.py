import unittest

from src.canary_probe import normalize_label


class NormalizeLabelTests(unittest.TestCase):
    def test_trims_surrounding_whitespace(self) -> None:
        self.assertEqual(normalize_label("  sample  "), "sample")


if __name__ == "__main__":
    unittest.main()
