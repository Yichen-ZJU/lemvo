"""Read-only context rendering regression. No model, GPU, or research gate."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/shared-references/scripts/construction_context.py"
spec = importlib.util.spec_from_file_location("construction_context", SCRIPT)
ctx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctx)


def finding(ident, supersedes=None, **values):
    result = dict(id=ident, candidate_id="H1", scope={"split": "dev", "recipe": "v1"},
                  interpretation="local observation", result_refs=["run1.json"])
    if supersedes is not None:
        result["supersedes"] = supersedes
    result.update(values)
    return result


class ContextTest(unittest.TestCase):
    def test_explicit_correction_preserves_measurements(self):
        old = finding("old", interpretation="family closed", measured=0.91)
        new = finding("new", ["old"], interpretation="closure retracted; untested relatives")
        active, history, warns = ctx.partition_findings([old, new])
        self.assertEqual(active, [new])
        self.assertEqual(history[0]["measured"], 0.91)
        self.assertEqual(warns, [])

    def test_chain_not_timestamp(self):
        records = [finding("a", timestamp="tomorrow"), finding("c", ["b"]), finding("b", ["a"])]
        active, history, _ = ctx.partition_findings(records)
        self.assertEqual([x["id"] for x in active], ["c"])
        self.assertEqual(len(history), 2)

    def test_cross_candidate_does_not_retract(self):
        a, h, w = ctx.partition_findings([finding("a"), finding("b", ["a"], candidate_id="H2")])
        self.assertEqual(len(a), 2)
        self.assertFalse(h)
        self.assertTrue(w)

    def test_cross_scope_positive_and_negative_remain(self):
        records = [finding("a", measured=3.97), finding("b", ["a"], scope="micro", measured=-1.28)]
        a, h, _ = ctx.partition_findings(records)
        self.assertEqual(len(a), 2)
        self.assertFalse(h)

    def test_missing_scope_is_not_global_authority(self):
        a, h, w = ctx.partition_findings([finding("a", scope=""), finding("b", "a", scope="")])
        self.assertEqual(len(a), 2)
        self.assertFalse(h)
        self.assertTrue(w)

    def test_duplicate_identity_is_ambiguous(self):
        records = [finding("a"), finding("a"), finding("b", ["a"])]
        a, h, w = ctx.partition_findings(records)
        self.assertEqual(a, records)
        self.assertFalse(h)
        self.assertTrue(w)

    def test_cycle_retained_even_with_incoming_link(self):
        records = [finding("a", ["b"]), finding("b", ["a"]), finding("c", ["a"])]
        a, h, w = ctx.partition_findings(records)
        self.assertEqual(a, records)
        self.assertFalse(h)
        self.assertTrue(w)

    def test_unknown_link_retains_record(self):
        a, h, w = ctx.partition_findings([finding("a", ["missing"])])
        self.assertEqual(len(a), 1)
        self.assertFalse(h)
        self.assertTrue(w)

    def test_original_candidate_missing_optional_fields(self):
        out = ctx.render_document('{"candidate_id":"H2","source_refs":[],"predicted_effect":"unknown"}', "Candidate")
        self.assertIn('"source_refs": []', out)
        self.assertIn('"predicted_effect": "unknown"', out)

    def test_findings_wrapper_metadata_is_preserved(self):
        out = ctx.render_findings({"scope_notes": "do not mix metrics", "findings": [finding("a")]})
        self.assertIn("do not mix metrics", out)

    def test_legacy_markdown_verbatim(self):
        text = "保留全历史，只mask低分目标loss。修订的是解释，不删除指标。"
        self.assertIn(text, ctx.render_document(text, "Old notes", findings=True))

    def test_embedded_findings_separated(self):
        value = dict(candidate_id="H1", concrete_change="collator.loss_mask", source_refs=["paper#3.2"],
                     findings=[finding("a"), finding("b", ["a"])])
        out = ctx.render_document(json.dumps(value), "Candidate")
        self.assertIn("collator.loss_mask", out)
        self.assertIn("paper#3.2", out)
        current = out.split("Current scoped interpretations", 1)[1].split("Historical interpretations", 1)[0]
        self.assertNotIn('"id": "a"', current)
        self.assertIn('"id": "b"', current)

    def test_cli_readonly_and_missing_file_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            original = b'{"candidate_id":"X","concrete_change":"loss_mask","unknown_field":7}'
            path.write_bytes(original)
            result = subprocess.run([sys.executable, str(SCRIPT), str(path), "--findings", str(path)+".missing"],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"unknown_field": 7', result.stdout)
            self.assertIn("Context unavailable", result.stdout)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main(verbosity=2)
