import unittest

from services.output_filter import OutputFilter


class TestOutputFilter(unittest.TestCase):
    def test_mixed_failure_output_preserves_warn_error_and_failed(self):
        text = "\n".join([
            "\x1b[36mcollecting benchmark output\x1b[0m",
            "tests/test_ok.py::test_a PASSED",
            "Downloading model shard 3/7...",
            "Downloading model shard 3/7...",
            "Downloading model shard 3/7...",
            "Downloading model shard 3/7...",
            "WARN cache miss while scanning cli/server.py",
            "ERROR failed benchmark step for cli/server.py",
            "FAILED tests/test_benchmark.py::test_000 - AssertionError: timed out on cli/server.py",
        ])
        result = OutputFilter({}).filter(text, use_llm=False)["filtered"]

        self.assertIn("WARN", result)
        self.assertIn("ERROR", result)
        self.assertIn("FAILED", result)

    def test_benchmark_fixture_preserves_repeat_marker(self):
        fixture = "\n".join(
            ["collecting benchmark output"]
            + [f"tests/test_{i:03d}.py::test_{i} PASSED" for i in range(20)]
            + ["progress heartbeat" for _ in range(8)]
            + [
                "WARN cache miss while scanning cli/server.py",
                "ERROR failed benchmark step for cli/server.py",
                "FAILED tests/test_benchmark.py::test_020 - AssertionError: timed out on cli/server.py",
            ]
        )
        result = OutputFilter({}).filter(fixture, use_llm=False)["filtered"]
        self.assertIn("[line repeated x", result)
        self.assertIn("WARN", result)


if __name__ == "__main__":
    unittest.main()
