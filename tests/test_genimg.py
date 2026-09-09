from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "generate2dspriteapi"
    / "scripts"
    / "genimg.py"
)
SPEC = importlib.util.spec_from_file_location("generate2dspriteapi_genimg", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SkillStructureTests(unittest.TestCase):
    def test_api_skill_only_owns_api_specific_files(self) -> None:
        api_root = SCRIPT_PATH.parents[1]
        shared_root = api_root.parent / "generate2dsprite"

        self.assertTrue((shared_root / "scripts" / "generate2dsprite.py").is_file())
        self.assertTrue((shared_root / "scripts" / "make_anchor_layout.py").is_file())
        self.assertTrue((shared_root / "scripts" / "make_layout_guide.py").is_file())
        self.assertTrue((api_root / "scripts" / "genimg.py").is_file())

        duplicated_files = [
            api_root / "scripts" / "generate2dsprite.py",
            api_root / "scripts" / "make_anchor_layout.py",
            api_root / "scripts" / "make_layout_guide.py",
            api_root / "references" / "modes.md",
            api_root / "references" / "prompt-rules.md",
        ]
        for duplicated_file in duplicated_files:
            self.assertFalse(duplicated_file.exists(), duplicated_file)


class ConfigTests(unittest.TestCase):
    def test_default_config_path_uses_codex_home(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME": "/tmp/codex-test"}, clear=False):
            self.assertEqual(
                MODULE.default_config_path(),
                Path("/tmp/codex-test/generate2dspriteapi.env"),
            )

    def test_config_file_loads_supported_values_without_executing_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "generate2dspriteapi.env"
            config_path.write_text(
                "# comments are ignored\n"
                "export OPENAI_API_KEY='file-key'\n"
                "OPENAI_BASE_URL=https://example.com/v1\n"
                "UNSUPPORTED=value\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                MODULE.load_config_file(config_path)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "file-key")
                self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://example.com/v1")
                self.assertNotIn("UNSUPPORTED", os.environ)

    def test_process_environment_overrides_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "generate2dspriteapi.env"
            config_path.write_text(
                "OPENAI_API_KEY=file-key\nOPENAI_BASE_URL=https://file.example/v1\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "process-key",
                    "OPENAI_BASE_URL": "https://process.example/v1",
                },
                clear=True,
            ):
                MODULE.load_config_file(config_path)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "process-key")
                self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://process.example/v1")

    def test_command_line_base_url_overrides_environment(self) -> None:
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://env.example/v1"}, clear=True):
            args = MODULE.parse_args(["duck", "--base-url", "https://cli.example/v1"])
            self.assertEqual(args.base_url, "https://cli.example/v1")

    def test_no_config_flag_is_available(self) -> None:
        args = MODULE.parse_args(["duck", "--no-config"])
        self.assertTrue(args.no_config)


if __name__ == "__main__":
    unittest.main()
