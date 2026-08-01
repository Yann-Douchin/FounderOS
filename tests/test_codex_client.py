from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from founder_os.agents.codex import CodexAppServerClient


class CodexAppServerClientTests(unittest.TestCase):
    def test_multiple_lines_in_one_flush_do_not_hide_the_response(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "fake-codex"
            executable.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys

                    for line in sys.stdin:
                        message = json.loads(line)
                        method = message.get("method")
                        if method == "initialize":
                            sys.stdout.write('{"method":"server/ready","params":{}}\\n')
                            sys.stdout.write('{"id":0,"result":{}}\\n')
                            sys.stdout.flush()
                        elif method == "account/rateLimits/read":
                            response = {
                                "id": message["id"],
                                "result": {
                                    "rateLimitsByLimitId": {
                                        "codex": {
                                            "primary": {
                                                "usedPercent": 25,
                                                "windowDurationMins": 300,
                                            }
                                        }
                                    }
                                },
                            }
                            print(json.dumps(response), flush=True)
                    """
                ),
                encoding="utf-8",
            )
            os.chmod(executable, 0o700)
            client = CodexAppServerClient(str(executable), timeout_seconds=2)
            try:
                result = client.read_rate_limits()
            finally:
                client.close()
            self.assertEqual(
                result["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"],
                25,
            )


if __name__ == "__main__":
    unittest.main()
