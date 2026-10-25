import unittest

from greeting import greeting


class GreetingTests(unittest.TestCase):
    def test_baseline_greeting(self):
        self.assertEqual(greeting(), "Hello")

    def test_result_is_text(self):
        self.assertIsInstance(greeting(), str)


if __name__ == "__main__":
    unittest.main()
