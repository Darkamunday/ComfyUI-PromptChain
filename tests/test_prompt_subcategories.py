import importlib
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class PromptSubcategoryTests(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault(
            "folder_paths",
            types.SimpleNamespace(get_user_directory=lambda: ""),
        )
        self.prompts = importlib.import_module("core.prompts")

    def test_subcategories_are_optional_and_scope_filtering_still_applies(self):
        fixture = Path(__file__).parent / "fixtures" / "prompt_subcategories_sample.json"
        with TemporaryDirectory() as tmp:
            system_dir = Path(tmp) / "system"
            system_dir.mkdir()
            (system_dir / fixture.name).write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

            self.prompts._system_dir = lambda: system_dir
            self.prompts._user_dir = lambda: Path(tmp) / "user"

            sdxl_prompts = self.prompts.list_prompts(architecture="sdxl")
            ids = {p["id"] for p in sdxl_prompts}
            self.assertIn("sample_global_hair_black", ids)
            self.assertIn("sample_sdxl_quality_soft_detail", ids)
            self.assertIn("sample_legacy_mood_warm", ids)

            by_id = {p["id"]: p for p in sdxl_prompts}
            self.assertEqual(by_id["sample_global_hair_black"]["subcategory"], "hair color")
            self.assertEqual(by_id["sample_sdxl_quality_soft_detail"]["subcategory"], "detail")
            self.assertNotIn("subcategory", by_id["sample_legacy_mood_warm"])

            flux_ids = {p["id"] for p in self.prompts.list_prompts(architecture="flux")}
            self.assertIn("sample_global_hair_black", flux_ids)
            self.assertIn("sample_legacy_mood_warm", flux_ids)
            self.assertNotIn("sample_sdxl_quality_soft_detail", flux_ids)


if __name__ == "__main__":
    unittest.main()
