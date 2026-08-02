from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EmulatorContractTests(unittest.TestCase):
    def test_firmware_merge_blocker_expiry_and_screen_contract(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            environment = dict(os.environ)
            environment["BUSY_DATA_DIR"] = folder
            result = subprocess.run(
                ["node", str(ROOT / "tests" / "emulator_contract_test.js")],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("emulator contract: ok", result.stdout)

    def test_all_bar_pilot_endpoints_and_methods_have_working_responses(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            environment = dict(os.environ)
            environment["BUSY_DATA_DIR"] = folder
            result = subprocess.run(
                ["node", str(ROOT / "tests" / "barpilot_endpoint_matrix_test.js")],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("53 endpoints, 69 operations ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
