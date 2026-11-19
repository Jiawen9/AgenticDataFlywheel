from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend.annotation_checkpoint import AnnotationCheckpoint


class AnnotationCheckpointTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.job = dict(batch_id="b1", pipeline_id="p1", job_id="j1", annotation_chain_id="j1",
                        fingerprint="model+input", input_sha256="snapshot")
        (self.root / "system/preprocessing/b1/j1").mkdir(parents=True)
        self.rows = [{"task_id": "t", "trajectory_id": tr, "step": 1} for tr in ("a", "b")]

    def checkpoint(self, **job_changes):
        record = AnnotationCheckpoint(self.root, {**self.job, **job_changes}, nullcontext)
        self.addCleanup(record.close)
        return record

    def test_chain_identity_sheet_order_and_model_output_blank_are_preserved(self):
        first = self.checkpoint()
        self.assertEqual(first.progress(), (0, 0))
        first.bind(self.rows, ["action"])
        first.save(3, "model_output_invalid", None, "empty_content")
        self.assertEqual(first.progress(), (1, 2))

        retry = self.checkpoint(job_id="j2")
        retry.bind(self.rows, ["action"])
        self.assertEqual(retry.completed, {3: ("model_output_invalid", None)})
        retry.save(2, "annotated", "click(box)")
        self.assertEqual(first.progress(), (2, 2))
        for changed_rows, columns in ((list(reversed(self.rows)), ["action"]), (self.rows, ["image"])):
            with self.subTest(columns=columns), self.assertRaisesRegex(ValueError, "行顺序"):
                self.checkpoint().bind(changed_rows, columns)
        for changes in ({"input_sha256": "changed"}, {"fingerprint": "other model"}, {"pipeline_id": "p2"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "不匹配"):
                self.checkpoint(**changes).progress()

    def test_stop_guard_does_not_commit_a_late_row(self):
        checkpoint = self.checkpoint()
        checkpoint.bind(self.rows, ["action"])
        def stopped():
            raise RuntimeError("stop accepted")
        checkpoint.commit_guard = stopped
        with self.assertRaisesRegex(RuntimeError, "stop accepted"):
            checkpoint.save(2, "no_box_needed", None)
        self.assertEqual(checkpoint.progress(), (0, 2))
