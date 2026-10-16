import unittest
from pathlib import Path
from mika_nexus.media import groups


class GalleryTests(unittest.TestCase):
    def test_partition_preserves_order(self):
        for n in (1, 2, 9, 10, 11, 12, 20, 21, 31):
            with self.subTest(n=n):
                paths = [Path(str(i)) for i in range(n)]
                chunks = groups(paths)
                self.assertEqual([p for chunk in chunks for p in chunk], paths)
                self.assertTrue(all(1 <= len(chunk) <= 10 for chunk in chunks))
                self.assertTrue(all(len(chunk) >= 2 for chunk in chunks[:-1]))
