from __future__ import annotations

import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from build_local_contract_snapshot import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    _inspect_source_entry,
    _stat_is_reparse_point,
    build_snapshot,
    sha256_file,
)


class SnapshotPackagerTests(unittest.TestCase):
    def make_skill(self, root: Path) -> Path:
        skill = root / "skill"
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: fixture\n---\n", encoding="utf-8")
        (skill / "references" / "说明.md").write_text("local contract\n", encoding="utf-8")
        (skill / "__pycache__").mkdir(); (skill / "__pycache__" / "x.pyc").write_bytes(b"ignored")
        (skill / "tests" / "out").mkdir(parents=True); (skill / "tests" / "out" / "receipt.json").write_text("{}")
        return skill

    def test_snapshot_is_deterministic_and_excludes_generated_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); skill = self.make_skill(root)
            first = build_snapshot(skill_root=skill, output_zip=root / "first.zip", receipt_path=root / "first.json")
            second = build_snapshot(skill_root=skill, output_zip=root / "second.zip", receipt_path=root / "second.json")
            self.assertEqual(first["source_tree_sha256"], second["source_tree_sha256"])
            self.assertEqual(sha256_file(root / "first.zip"), sha256_file(root / "second.zip"))
            with zipfile.ZipFile(root / "first.zip") as archive:
                names = archive.namelist()
                self.assertIn("package_manifest.json", names)
                self.assertIn("alibaba-similar-draft/references/说明.md", names)
                self.assertFalse(any("__pycache__" in name or "/out/" in name for name in names))
                manifest = json.loads(archive.read("package_manifest.json"))
                self.assertEqual(manifest["trust_status"], "PACKAGE_INTEGRITY_ONLY")
                self.assertEqual(manifest["quality_acceptance_status"], "NOT_ASSERTED_BY_PACKAGER")
                self.assertFalse(manifest["implementation_ready"])
                self.assertFalse(manifest["final_write_enabled"])
            self.assertEqual(first["status"], "PASS_PACKAGE_INTEGRITY")
            self.assertEqual(first["trust_status"], "PACKAGE_INTEGRITY_ONLY")

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); skill = self.make_skill(root)
            output = root / "snapshot.zip"; output.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                build_snapshot(skill_root=skill, output_zip=output, receipt_path=root / "receipt.json")
            self.assertEqual(output.read_bytes(), b"keep")

    def test_secret_bearing_filename_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); skill = self.make_skill(root)
            (skill / "cookies.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "forbidden secret-bearing"):
                build_snapshot(skill_root=skill, output_zip=root / "snapshot.zip", receipt_path=root / "receipt.json")

    def test_unknown_top_level_run_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); skill = self.make_skill(root)
            private_run = skill / "private-run"
            private_run.mkdir()
            (private_run / "approved_source_manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unapproved top-level directory"):
                build_snapshot(skill_root=skill, output_zip=root / "snapshot.zip", receipt_path=root / "receipt.json")

    def test_package_outputs_inside_skill_root_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); skill = self.make_skill(root)
            with self.assertRaisesRegex(ValueError, "outside the Skill root"):
                build_snapshot(
                    skill_root=skill,
                    output_zip=skill / "snapshot.zip",
                    receipt_path=root / "receipt.json",
                )
            with self.assertRaisesRegex(ValueError, "outside the Skill root"):
                build_snapshot(
                    skill_root=skill,
                    output_zip=root / "snapshot.zip",
                    receipt_path=skill / "receipt.json",
                )
            self.assertFalse((skill / "snapshot.zip").exists())
            self.assertFalse((skill / "receipt.json").exists())

    def test_file_symlink_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); skill = self.make_skill(root)
            outside = root / "outside.md"
            outside.write_text("must not be packaged\n", encoding="utf-8")
            link = skill / "references" / "outside-link.md"
            try:
                link.symlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"file symlink creation is unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "symlink, junction, or reparse point"):
                build_snapshot(
                    skill_root=skill,
                    output_zip=root / "snapshot.zip",
                    receipt_path=root / "receipt.json",
                )

    def test_directory_symlink_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); skill = self.make_skill(root)
            outside = root / "outside-directory"
            outside.mkdir()
            (outside / "private.json").write_text("{}", encoding="utf-8")
            link = skill / "references" / "outside-directory-link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"directory symlink creation is unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "symlink, junction, or reparse point"):
                build_snapshot(
                    skill_root=skill,
                    output_zip=root / "snapshot.zip",
                    receipt_path=root / "receipt.json",
                )

    def test_skill_root_symlink_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); skill = self.make_skill(root)
            linked_skill = root / "linked-skill"
            try:
                linked_skill.symlink_to(skill, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"Skill-root symlink creation is unavailable: {exc}")
            with self.assertRaisesRegex(
                ValueError, "Skill root must not be a symlink, junction, or reparse point"
            ):
                build_snapshot(
                    skill_root=linked_skill,
                    output_zip=root / "snapshot.zip",
                    receipt_path=root / "receipt.json",
                )

    def test_windows_reparse_attribute_is_detected_cross_platform(self) -> None:
        metadata = SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT,
        )
        self.assertTrue(_stat_is_reparse_point(metadata))

    def test_resolved_entry_outside_skill_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); skill = self.make_skill(root)
            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            parent_escape = skill / ".." / outside.name
            with self.assertRaisesRegex(ValueError, "resolves outside the Skill root"):
                _inspect_source_entry(parent_escape, skill.resolve(strict=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
