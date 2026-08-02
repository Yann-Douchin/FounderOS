from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from apps import founderosctl
from founder_os.closure import ClosureEngine, Gate, Obligation, ObligationLedger
from founder_os.models import Event, utc_now


class OperatorCLITests(unittest.TestCase):
    def test_next_action_and_delegate_are_persisted_with_audit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "obligations.sqlite3"
            ledger = ObligationLedger(path)
            obligation = Obligation(
                id="",
                key="meeting:test",
                title="Partner review | ASSIGN NEXT MOVE",
                gates=(Gate(name="next_move"),),
            )
            ledger.upsert(obligation)
            ledger.close()
            config = {"closure": {"ledger_path": str(path)}}
            output = io.StringIO()
            with redirect_stdout(output):
                founderosctl._obligation_command(SimpleNamespace(
                    obligation_command="action",
                    id=obligation.id,
                    text="Send the validated proposal",
                    actor="Yann",
                ), config)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["next_actor"], "Yann")
            ledger = ObligationLedger(path)
            changed = ledger.get(obligation.id)
            self.assertEqual(changed.metadata["next_action"], "Send the validated proposal")
            self.assertIn("next_actor", changed.metadata["manual_correction"]["fields"])
            self.assertEqual(next(gate for gate in changed.gates if gate.name == "next_move").state, "satisfied")
            self.assertEqual(ledger.audit(obligation.id)[0]["reason"], "manual next action recorded")
            ledger.close()
            with redirect_stdout(io.StringIO()):
                founderosctl._obligation_command(SimpleNamespace(
                    obligation_command="delegate",
                    id=obligation.id,
                    actor="Sam",
                ), config)
            ledger = ObligationLedger(path)
            delegated = ledger.get(obligation.id)
            self.assertEqual(delegated.metadata["delegate"], "Sam")
            self.assertTrue(ledger.transitions(obligation.id))
            self.assertEqual(ledger.audit(obligation.id)[0]["reason"], "manual metadata correction by founderosctl")
            ledger.close()

    def test_relationship_correction_is_audited_visible_and_source_stable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "obligations.sqlite3"
            config = {"closure": {"ledger_path": str(path)}}
            with redirect_stdout(io.StringIO()):
                founderosctl._relationship_command(SimpleNamespace(
                    relationship_command="set",
                    key="partner.example",
                    name="Partenaire Étoile",
                    stage="design_partner",
                    next_decision="Approve rollout",
                    resume_after=None,
                    cooling_off_until=None,
                ), config)
            output = io.StringIO()
            with redirect_stdout(output):
                founderosctl._relationship_command(SimpleNamespace(
                    relationship_command="show",
                    key="partner.example",
                ), config)
            shown = json.loads(output.getvalue())
            self.assertEqual(shown["name"], "Partenaire Étoile")
            self.assertEqual(shown["next_decision"], "Approve rollout")
            self.assertEqual(shown["audit"][0]["reason"], "manual relationship correction")
            ledger = ObligationLedger(path)
            engine = ClosureEngine({
                "enabled": True,
                "default_owner": "Yann",
                "self_aliases": ["Yann"],
                "timezone": "Europe/Madrid",
            }, ledger)
            now = utc_now()
            engine.reconcile([
                Event(
                    source="gmail",
                    id="gmail:relationship-source",
                    title="Source name: Customer feedback",
                    action_required=True,
                    occurred_at=now,
                    metadata={
                        "counterparty": "Source name",
                        "relationship_key": "partner.example",
                        "feedback": True,
                        "owner": "Yann",
                    },
                )
            ], now)
            relationship = ledger.relationship("partner.example")
            self.assertEqual(relationship.name, "Partenaire Étoile")
            self.assertEqual(relationship.next_decision, "Approve rollout")
            engine.close()


if __name__ == "__main__":
    unittest.main()
