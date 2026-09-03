"""`c3 index` must not shadow index_max_files with a hard-coded cap.

The bug this guards: `cmd_index` called ``build_index(max_files=args.max_files
or 500)`` while the parser defaulted ``--max-files`` to 500. Both halves had
to be wrong together, and they were — every `c3 index` run capped at 500
files no matter what ``index_max_files`` said in ``.c3/config.json``, and the
rich table reported the truncated total as if it were the whole repo. On a
1933-file tree that produced a quarter-sized index whose symbol map still
referenced chunks the run never wrote, so searches raised KeyError.

Asserting "the number 500 is absent" would be the drifting list in a costume.
These tests assert the invariant instead: an unset flag reaches the indexer as
None (the only value that means "read the config"), an explicit flag is
honoured verbatim, and a truncated run says so out loud.
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli.commands.common import CommandDeps, cmd_index
from cli.commands.parser import build_parser


class _RecordingIndex:
    """Stands in for CodeIndex; records the cap it was handed."""

    last_max_files = "unset"

    def __init__(self, project_path, result=None):
        self.project_path = project_path
        self._result = result or {
            "files_indexed": 7,
            "chunks_created": 70,
            "unique_symbols": 12,
            "files_capped": 0,
            "max_files": 2000,
        }

    def build_index(self, max_files=None, on_progress=None):
        type(self).last_max_files = max_files
        return self._result


def _deps(index_factory):
    """CommandDeps with rich disabled so assertions read plain stdout."""
    return CommandDeps(
        load_config=lambda: {"project_path": "."},
        print_header=lambda *a, **k: None,
        print_savings=lambda *a, **k: None,
        count_tokens=lambda *a, **k: 0,
        format_token_count=lambda n: str(n),
        CodeIndex=index_factory,
        CodeCompressor=object,
        CompressionProtocol=object,
        SessionManager=object,
        HAS_RICH=False,
        Table=object,
        console=None,
        __file__=__file__,
    )


def _parse(argv):
    return build_parser("0.0-test", lambda value: value).parse_args(argv)


class IndexCapTests(unittest.TestCase):
    def test_unset_flag_reaches_the_indexer_as_none(self):
        """None is the only value that lets build_index read index_max_files."""
        args = _parse(["index"])
        self.assertIsNone(
            args.max_files,
            "--max-files must default to None; any number here shadows "
            "index_max_files for everyone who does not type the flag",
        )

        _RecordingIndex.last_max_files = "unset"
        with redirect_stdout(io.StringIO()):
            cmd_index(args, _deps(_RecordingIndex))

        self.assertIsNone(_RecordingIndex.last_max_files)

    def test_an_explicit_flag_is_passed_through_verbatim(self):
        args = _parse(["index", "--max-files", "3000"])
        _RecordingIndex.last_max_files = "unset"
        with redirect_stdout(io.StringIO()):
            cmd_index(args, _deps(_RecordingIndex))

        self.assertEqual(_RecordingIndex.last_max_files, 3000)

    def test_a_truncated_index_says_so(self):
        """A partial index that reports only its own total is fail-stale."""
        capped_result = {
            "files_indexed": 500,
            "chunks_created": 5488,
            "unique_symbols": 1552,
            "files_capped": 1433,
            "max_files": 500,
        }

        def factory(project_path):
            return _RecordingIndex(project_path, result=capped_result)

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_index(_parse(["index", "--max-files", "500"]), _deps(factory))
        out = buf.getvalue()

        self.assertIn("500 of 1933", out, "the population must appear beside the count")
        self.assertIn("index_max_files", out, "name the knob that caused it")

    def test_a_complete_index_does_not_cry_wolf(self):
        """No warning when nothing was skipped, or the warning gets ignored."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_index(_parse(["index"]), _deps(_RecordingIndex))
        out = buf.getvalue()

        self.assertNotIn("[!]", out)
        self.assertNotIn("index_max_files", out)


if __name__ == "__main__":
    unittest.main()
