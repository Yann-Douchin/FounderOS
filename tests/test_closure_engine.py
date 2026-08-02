from __future__ import annotations

import json
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from founder_os.closure import ClosureEngine, Obligation, ObligationLedger, Relationship
from founder_os.models import Event


UTC = timezone.utc
NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)


def config(**overrides) -> dict:
    value = {
        "enabled": True,
        "default_owner": "Yann",
        "self_aliases": ["self", "me", "Yann"],
        "timezone": "Europe/Madrid",
        "source_priority_cap": 72,
        "burst_window_minutes": 240,
        "burst_threshold": 4,
        "stale_after_days": 45,
        "evidence_ttl_hours": 168,
        "event_lease_seconds": 180,
        "capacity": {"due_day_threshold": 3, "require_handoff_when_unavailable": True},
        "proof_profiles": {
            "release": {
                "required_categories": ["deployment", "analytics", "market", "language", "pricing", "device"],
                "minimum_categories": 6,
            },
            "commitment": {"required_categories": [], "minimum_categories": 0},
            "meeting": {"required_categories": [], "minimum_categories": 0},
            "feedback": {"required_categories": [], "minimum_categories": 0},
            "decision": {"required_categories": [], "minimum_categories": 0},
        },
    }
    value.update(overrides)
    return value


class ClosureEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.ledger = ObligationLedger(self.root / "obligations.sqlite3")
        self.engine = ClosureEngine(config(snapshot_path=str(self.root / "obligations.json")), self.ledger)

    def tearDown(self) -> None:
        self.engine.close()
        self.folder.cleanup()

    def test_commitment_ledger_tracks_owner_counterparty_and_next_actor(self) -> None:
        events = self.engine.reconcile([
            Event(
                source="gmail",
                id="gmail:thread-1",
                title="I will send the signed proposal tomorrow",
                kind="email",
                action_required=True,
                due_at=NOW + timedelta(days=1),
                occurred_at=NOW,
                metadata={
                    "direction": "outgoing",
                    "thread_id": "thread-1",
                    "counterparty": "Design Partner",
                    "sender_domain": "partner.example",
                },
            )
        ], NOW)
        self.assertEqual(len(events), 1)
        obligation = self.ledger.get(events[0].metadata["obligation_id"])
        self.assertEqual(obligation.owner, "Yann")
        self.assertEqual(obligation.counterparty, "Design Partner")
        self.assertEqual(obligation.next_actor, "Yann")
        self.assertEqual(obligation.due_at, NOW + timedelta(days=1))

    def test_incoming_slack_author_is_counterparty_not_action_owner(self) -> None:
        output = self.engine.reconcile([
            Event(
                source="slack",
                id="slack:incoming-owner",
                title="Please confirm the launch date",
                action_required=True,
                occurred_at=NOW,
                metadata={
                    "direction": "incoming",
                    "sender_name": "Élodie",
                    "thread_id": "incoming-owner",
                },
            )
        ], NOW)
        obligation = self.ledger.get(output[0].metadata["obligation_id"])
        self.assertEqual(obligation.owner, "Yann")
        self.assertEqual(obligation.counterparty, "Élodie")
        self.assertEqual(obligation.next_actor, "Yann")

    def test_relative_deadline_and_named_dependency_are_deterministic(self) -> None:
        output = self.engine.reconcile([
            Event(
                source="slack",
                id="slack:dependency",
                title="Waiting for Élodie, I will send the proof tomorrow",
                action_required=True,
                occurred_at=NOW,
                metadata={"thread_id": "dependency"},
            )
        ], NOW)
        obligation = self.ledger.get(output[0].metadata["obligation_id"])
        self.assertEqual(obligation.next_actor, "Élodie")
        self.assertEqual(obligation.due_at.astimezone(self.engine.timezone).hour, 18)
        self.assertEqual(obligation.due_at.astimezone(self.engine.timezone).date(), (NOW.astimezone(self.engine.timezone) + timedelta(days=1)).date())

    def test_false_ready_remains_blocked_until_every_operational_gate_is_closed(self) -> None:
        events = self.engine.reconcile([
            Event(
                source="linear",
                id="linear:release",
                title="Merchant Console ready, need access before go-live",
                kind="blocker",
                action_required=True,
                priority=100,
                occurred_at=NOW,
                metadata={"project_id": "merchant", "project": "Merchant Console"},
            )
        ], NOW)
        obligation = self.ledger.get(events[0].metadata["obligation_id"])
        gates = {gate.name: gate.state for gate in obligation.gates}
        self.assertEqual(gates["code"], "satisfied")
        self.assertEqual(gates["access"], "blocked")
        self.assertNotEqual(gates["evidence"], "satisfied")
        self.assertEqual(obligation.state, "blocked")
        self.assertIn("ACCESS BLOCKED", obligation.title)

    def test_evidence_quorum_closes_the_proof_gate_across_sources(self) -> None:
        base = Event(
            source="linear",
            id="linear:launch",
            title="Launch ready",
            kind="deadline",
            action_required=True,
            occurred_at=NOW,
            metadata={
                "project_id": "launch",
                "project": "Launch",
                "gate_status": {"access": "satisfied", "validation": "satisfied", "deployment": "satisfied"},
            },
        )
        evidence = [
            Event(
                source="sheets",
                id=f"sheets:{category}",
                title=f"{category} validated",
                occurred_at=NOW,
                metadata={
                    "project_id": "launch",
                    "project": "Launch",
                    "evidence_categories": [category],
                    "evidence_status": "present",
                },
            )
            for category in ("deployment", "analytics", "market", "language", "pricing", "device")
        ]
        output = self.engine.reconcile([base, *evidence], NOW)
        obligation = self.ledger.get(output[0].metadata["obligation_id"])
        gates = {gate.name: gate for gate in obligation.gates}
        self.assertEqual(gates["evidence"].state, "satisfied")
        self.assertEqual(len(gates["evidence"].evidence_ids), 6)
        self.assertEqual(obligation.state, "ready")

    def test_capacity_concentration_and_absence_require_a_handoff(self) -> None:
        due = NOW + timedelta(days=1)
        obligations = [
            Event(
                source="linear",
                id=f"linear:{index}",
                title=f"Task {index}",
                action_required=True,
                due_at=due,
                occurred_at=NOW,
                metadata={"assignee": "Alex"},
            )
            for index in range(3)
        ]
        obligations.append(Event(
            source="calendar",
            id="calendar:ooo",
            title="Alex vacation",
            occurred_at=NOW,
            expires_at=due + timedelta(days=1),
            metadata={"availability": "unavailable", "person": "Alex", "start_at": NOW, "end_at": due + timedelta(days=1)},
        ))
        output = self.engine.reconcile(obligations, NOW)
        self.assertEqual(len(output), 3)
        for event in output:
            obligation = self.ledger.get(event.metadata["obligation_id"])
            gates = {gate.name: gate.state for gate in obligation.gates}
            self.assertEqual(gates["capacity"], "blocked")
            self.assertEqual(gates["handoff"], "blocked")
            self.assertIn("NO BACKUP", obligation.title)

    def test_recorded_delegate_satisfies_absence_handoff(self) -> None:
        due = NOW + timedelta(days=1)
        work = Event(
            source="linear",
            id="linear:delegated",
            title="Deliver proposal",
            action_required=True,
            due_at=due,
            occurred_at=NOW,
            metadata={"assignee": "Alex"},
        )
        absence = Event(
            source="calendar",
            id="calendar:alex-away",
            title="Alex vacation",
            occurred_at=NOW,
            expires_at=due + timedelta(days=1),
            metadata={"availability": "unavailable", "person": "Alex", "start_at": NOW, "end_at": due + timedelta(days=1)},
        )
        first = self.engine.reconcile([work, absence], NOW)[0]
        self.ledger.correct_metadata(first.metadata["obligation_id"], {"delegate": "Sam"}, now=NOW)
        second = self.engine.reconcile([work, absence], NOW)[0]
        obligation = self.ledger.get(second.metadata["obligation_id"])
        gates = {gate.name: gate.state for gate in obligation.gates}
        self.assertNotIn("capacity", gates)
        self.assertEqual(gates["handoff"], "satisfied")
        self.assertEqual(obligation.next_actor, "Sam")

    def test_burst_compaction_caps_source_priority_and_emits_one_outcome(self) -> None:
        events = [
            Event(
                source="linear" if index % 2 == 0 else "slack",
                id=f"source:{index}",
                title=f"Launch change {index}",
                action_required=True,
                priority=100,
                occurred_at=NOW + timedelta(minutes=index),
                metadata={"project_id": "launch", "project": "Launch"},
            )
            for index in range(6)
        ]
        output = self.engine.reconcile(events, NOW + timedelta(minutes=6))
        self.assertEqual(len(output), 1)
        obligation = self.ledger.get(output[0].metadata["obligation_id"])
        self.assertEqual(obligation.burst_count, 6)
        self.assertTrue(obligation.metadata["priority_diluted"])
        self.assertEqual(obligation.metadata["source_priority_normalized"], 72)
        self.assertLessEqual(obligation.priority, 97)

    def test_meeting_transition_changes_from_decision_to_next_move(self) -> None:
        before = Event(
            source="calendar",
            id="calendar:meeting:before",
            title="PREP Customer review",
            kind="meeting",
            action_required=True,
            occurred_at=NOW,
            metadata={"meeting_id": "meeting-1", "meeting_phase": "before"},
        )
        output = self.engine.reconcile([before], NOW)
        self.assertIn("NEED DECISION", output[0].title)
        after = Event(
            source="calendar",
            id="calendar:meeting:after",
            title="Customer review follow-up",
            kind="meeting",
            action_required=True,
            occurred_at=NOW + timedelta(hours=1),
            metadata={"meeting_id": "meeting-1", "meeting_phase": "after"},
        )
        output = self.engine.reconcile([after], NOW + timedelta(hours=1))
        self.assertIn("ASSIGN NEXT MOVE", output[0].title)

    def test_relationship_cooling_period_suppresses_inappropriate_followup(self) -> None:
        self.ledger.upsert_relationship(Relationship(
            key="partner.example",
            name="Partner",
            cooling_off_until=NOW + timedelta(days=3),
        ))
        output = self.engine.reconcile([
            Event(
                source="gmail",
                id="gmail:cooling",
                title="Following up on our proposal",
                action_required=True,
                occurred_at=NOW,
                metadata={"sender_domain": "partner.example", "counterparty": "Partner"},
            )
        ], NOW)
        self.assertEqual(output, [])
        obligation = self.ledger.list(active_only=True)[0]
        self.assertEqual(obligation.state, "deferred")

    def test_customer_feedback_without_owner_or_decision_is_the_only_alert(self) -> None:
        output = self.engine.reconcile([
            Event(
                source="slack",
                id="slack:feedback",
                title="Design partner feedback: export needs filtering",
                action_required=False,
                occurred_at=NOW,
                metadata={"customer": "Partner", "feedback": True},
            )
        ], NOW)
        obligation = self.ledger.get(output[0].metadata["obligation_id"])
        gates = {gate.name: gate.state for gate in obligation.gates}
        self.assertEqual(gates, {"decision": "pending", "ownership": "blocked"})
        self.assertIn("NO OWNER", obligation.title)

    def test_routed_customer_feedback_closes_without_an_alert(self) -> None:
        output = self.engine.reconcile([
            Event(
                source="sheets",
                id="sheets:routed-feedback",
                title="Customer feedback approved for roadmap",
                action_required=False,
                occurred_at=NOW,
                metadata={
                    "customer": "Partner",
                    "feedback": True,
                    "owner": "Yann",
                    "decision_id": "decision-1",
                },
            )
        ], NOW)
        self.assertEqual(output, [])
        obligation = self.ledger.list(active_only=False)[0]
        self.assertEqual(obligation.state, "closed")
        self.assertIsNotNone(obligation.closed_at)

    def test_verified_linear_reference_routes_customer_feedback(self) -> None:
        output = self.engine.reconcile([
            Event(
                source="linear",
                id="linear:issue-42",
                title="QTY-42 Add export filtering",
                action_required=True,
                occurred_at=NOW,
                metadata={
                    "identifier": "QTY-42",
                    "assignee": "Alex",
                    "project": "Exports",
                },
            ),
            Event(
                source="slack",
                id="slack:linked-feedback",
                title="Customer feedback is tracked in QTY-42",
                action_required=False,
                occurred_at=NOW,
                metadata={"customer": "Partner", "feedback": True},
            ),
        ], NOW)
        self.assertEqual(len(output), 1)
        feedback = self.ledger.for_event("slack:linked-feedback")
        self.assertEqual(feedback.state, "closed")
        self.assertEqual(feedback.owner, "Alex")
        self.assertEqual(feedback.project, "Exports")
        self.assertEqual(feedback.metadata["roadmap_issue_ids"], ["QTY-42"])

    def test_unverified_ticket_text_does_not_route_customer_feedback(self) -> None:
        output = self.engine.reconcile([
            Event(
                source="slack",
                id="slack:unverified-feedback",
                title="Customer feedback might relate to QTY-404",
                action_required=False,
                occurred_at=NOW,
                metadata={"customer": "Partner", "feedback": True},
            )
        ], NOW)
        self.assertEqual(len(output), 1)
        feedback = self.ledger.for_event("slack:unverified-feedback")
        self.assertEqual(feedback.state, "blocked")
        self.assertEqual(feedback.metadata["roadmap_issue_ids"], [])

    def test_negative_readiness_language_never_satisfies_a_gate_or_proof(self) -> None:
        output = self.engine.reconcile([
            Event(
                source="linear",
                id="linear:negative-ready",
                title="Launch not ready, not deployed, access not granted and not validated",
                kind="blocker",
                action_required=True,
                occurred_at=NOW,
                metadata={"project": "Launch"},
            )
        ], NOW)
        obligation = self.ledger.get(output[0].metadata["obligation_id"])
        gates = {gate.name: gate.state for gate in obligation.gates}
        self.assertNotIn("satisfied", {gates["code"], gates["deployment"], gates["access"], gates["validation"]})
        self.assertFalse(any(item.category in {"deployment", "access", "validation"} for item in obligation.evidence))

    def test_explicit_gate_state_wins_over_conflicting_title_language(self) -> None:
        output = self.engine.reconcile([
            Event(
                source="linear",
                id="linear:explicit-gates",
                title="Launch ready, deployed and access not granted",
                action_required=True,
                occurred_at=NOW,
                metadata={
                    "project": "Launch",
                    "gate_status": {
                        "code": "blocked",
                        "deployment": "blocked",
                        "access": "satisfied",
                    },
                },
            )
        ], NOW)
        obligation = self.ledger.get(output[0].metadata["obligation_id"])
        gates = {gate.name: gate.state for gate in obligation.gates}
        self.assertEqual(gates["code"], "blocked")
        self.assertEqual(gates["deployment"], "blocked")
        self.assertEqual(gates["access"], "satisfied")

    def test_word_boundaries_prevent_unblocked_and_preproduction_false_signals(self) -> None:
        output = self.engine.reconcile([
            Event(
                source="linear",
                id="linear:unblocked-closure",
                title="Cleanup is unblocked",
                action_required=True,
                occurred_at=NOW,
                metadata={"state": "Unblocked"},
            ),
            Event(
                source="github",
                id="github:preproduction-ci",
                title="Preproduction CI failed",
                kind="incident",
                action_required=True,
                occurred_at=NOW,
            ),
        ], NOW)
        self.assertEqual(len(output), 2)
        unblocked = self.ledger.for_event("linear:unblocked-closure")
        preproduction = self.ledger.for_event("github:preproduction-ci")
        self.assertEqual(unblocked.metadata["profile"], "commitment")
        self.assertEqual(preproduction.metadata["profile"], "commitment")
        delivery = next(gate for gate in unblocked.gates if gate.name == "delivery")
        self.assertNotEqual(delivery.state, "blocked")

    def test_manual_close_does_not_reopen_until_the_source_changes(self) -> None:
        event = Event(source="gmail", id="gmail:close", title="Please confirm", action_required=True, occurred_at=NOW)
        output = self.engine.reconcile([event], NOW)
        obligation_id = output[0].metadata["obligation_id"]
        self.ledger.transition(obligation_id, "closed", reason="operator", now=NOW + timedelta(minutes=1))
        self.assertEqual(self.engine.reconcile([event], NOW + timedelta(minutes=2)), [])
        changed = Event(source="gmail", id="gmail:close", title="Please confirm again", action_required=True, occurred_at=NOW + timedelta(minutes=3))
        reopened = self.engine.reconcile([changed], NOW + timedelta(minutes=3))
        self.assertEqual(len(reopened), 1)

    def test_non_actionable_source_update_can_complete_an_existing_gate(self) -> None:
        pending = Event(
            source="notion",
            id="notion:decision-update",
            title="Choose rollout market",
            action_required=True,
            occurred_at=NOW,
            metadata={"obligation_type": "decision"},
        )
        first = self.engine.reconcile([pending], NOW)[0]
        decided = Event(
            source="notion",
            id="notion:decision-update",
            title="Choose rollout market",
            action_required=False,
            occurred_at=NOW + timedelta(minutes=1),
            metadata={
                "obligation_type": "decision",
                "decision": "Spain first",
            },
        )
        second = self.engine.reconcile([decided], NOW + timedelta(minutes=1))[0]
        self.assertEqual(second.metadata["obligation_id"], first.metadata["obligation_id"])
        obligation = self.ledger.get(second.metadata["obligation_id"])
        self.assertEqual(obligation.state, "ready")
        self.assertEqual(next(gate for gate in obligation.gates if gate.name == "decision").state, "satisfied")

    def test_ledger_is_private_audited_and_exports_valid_utf8(self) -> None:
        obligation = Obligation(id="", key="test", title="Décision", updated_at=NOW)
        self.ledger.upsert(obligation)
        self.ledger.transition(obligation.id, "closed", reason="validé", now=NOW)
        mode = stat.S_IMODE(self.ledger.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.ledger.transitions(obligation.id)[0]["reason"], "validé")
        export = self.root / "snapshot.json"
        self.ledger.export_snapshot(export, now=NOW)
        payload = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)

    def test_runtime_snapshot_contains_only_the_governed_projection(self) -> None:
        stale = Event(
            source="linear",
            id="linear:stale-snapshot",
            title="Ancienne obligation",
            action_required=True,
            occurred_at=NOW - timedelta(days=90),
        )
        governed = Event(
            source="gmail",
            id="gmail:current-snapshot",
            title="Décision actuelle",
            action_required=True,
            occurred_at=NOW,
        )
        self.engine.reconcile([stale, governed], NOW)
        payload = json.loads((self.root / "obligations.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["obligations"]), 1)
        self.assertTrue(payload["obligations"][0]["title"].startswith("Décision actuelle"))
        self.assertEqual(len(self.ledger.list(active_only=False)), 1)

    def test_open_obligation_never_ages_out_after_it_enters_the_ledger(self) -> None:
        source = Event(
            source="gmail",
            id="gmail:durable-commitment",
            title="Je vous envoie la proposition demain",
            action_required=True,
            occurred_at=NOW,
            metadata={"direction": "outgoing", "thread_id": "durable-commitment"},
        )
        self.engine.reconcile([source], NOW)
        future = NOW + timedelta(days=90)
        output = self.engine.reconcile([], future)
        self.assertEqual(len(output), 1)
        self.assertEqual(len(self.ledger.list(active_only=True)), 1)
        payload = json.loads((self.root / "obligations.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["obligations"]), 1)

    def test_gate_resolution_is_independent_of_connector_event_order(self) -> None:
        ready = Event(
            source="github",
            id="github:ordered-ready",
            title="Launch deployed",
            action_required=True,
            occurred_at=NOW + timedelta(minutes=1),
            metadata={
                "project": "Launch",
                "gate_status": {"deployment": "satisfied"},
            },
        )
        failed = Event(
            source="deployment",
            id="deployment:ordered-failed",
            title="Launch deployment failed",
            action_required=True,
            occurred_at=NOW,
            metadata={
                "project": "Launch",
                "gate_status": {"deployment": "blocked"},
            },
        )
        for ordered in ([ready, failed], [failed, ready]):
            output = self.engine.reconcile(ordered, NOW + timedelta(minutes=1))
            obligation = self.ledger.get(output[0].metadata["obligation_id"])
            deployment = next(gate for gate in obligation.gates if gate.name == "deployment")
            self.assertEqual(deployment.state, "satisfied")

    def test_structured_source_evidence_gets_the_default_expiry(self) -> None:
        source = Event(
            source="sheets",
            id="sheets:scoped-expiry",
            title="Proof matrix",
            action_required=True,
            occurred_at=NOW,
            metadata={
                "project": "Launch",
                "evidence": [{"category": "market", "scope": "FR"}],
            },
        )
        output = self.engine.reconcile([source], NOW)
        evidence = self.ledger.get(output[0].metadata["obligation_id"]).evidence
        self.assertEqual(evidence[0].expires_at, NOW + timedelta(hours=168))

    def test_inferred_relationship_decision_clears_when_the_gate_closes(self) -> None:
        pending = Event(
            source="slack",
            id="slack:relationship-decision",
            title="Partner decision required",
            action_required=True,
            occurred_at=NOW,
            metadata={
                "customer": "Partner",
                "feedback": True,
                "owner": "Yann",
                "relationship_key": "partner.example",
            },
        )
        self.engine.reconcile([pending], NOW)
        self.assertTrue(self.ledger.relationship("partner.example").next_decision)
        decided = Event(
            source="slack",
            id="slack:relationship-decision",
            title="Partner feedback routed",
            action_required=False,
            occurred_at=NOW + timedelta(minutes=1),
            metadata={
                "customer": "Partner",
                "feedback": True,
                "owner": "Yann",
                "decision_id": "decision-1",
                "relationship_key": "partner.example",
            },
        )
        self.engine.reconcile([decided], NOW + timedelta(minutes=1))
        self.assertEqual(self.ledger.relationship("partner.example").next_decision, "")

    def test_sequential_manual_corrections_keep_every_field_pinned(self) -> None:
        obligation = Obligation(id="", key="manual-pins", title="Correct me", updated_at=NOW)
        self.ledger.upsert(obligation)
        self.ledger.correct(obligation.id, {"owner": "Yann"}, now=NOW)
        corrected = self.ledger.correct(
            obligation.id,
            {"due_at": (NOW + timedelta(days=1)).isoformat()},
            now=NOW + timedelta(minutes=1),
        )
        self.assertEqual(
            corrected.metadata["manual_correction"]["fields"],
            ["due_at", "owner"],
        )

    def test_scoped_proof_quorum_requires_every_configured_market_and_language(self) -> None:
        scoped = config(
            snapshot_path=str(self.root / "scoped.json"),
            proof_profiles={
                "release": {
                    "required_categories": ["market", "language"],
                    "minimum_categories": 2,
                    "required_scopes": {"market": ["FR", "ES"], "language": ["fr", "es"]},
                },
            },
        )
        engine = ClosureEngine(scoped, self.ledger)
        base = Event(
            source="linear",
            id="linear:scoped-release",
            title="Launch ready",
            action_required=True,
            occurred_at=NOW,
            metadata={"project": "Launch", "gate_status": {"code": "satisfied", "deployment": "satisfied", "access": "satisfied", "validation": "satisfied"}},
        )
        evidence = Event(
            source="sheets",
            id="sheets:partial-proof",
            title="Partial proof matrix",
            occurred_at=NOW,
            metadata={
                "project": "Launch",
                "evidence": [
                    {"category": "market", "scope": "FR"},
                    {"category": "language", "scope": "fr"},
                    {"category": "language", "scope": "es"},
                ],
            },
        )
        output = engine.reconcile([base, evidence], NOW)
        obligation = self.ledger.get(output[0].metadata["obligation_id"])
        proof = next(gate for gate in obligation.gates if gate.name == "evidence")
        self.assertEqual(proof.state, "blocked")
        self.assertIn("market:es", proof.detail)

    def test_evidence_minimum_allows_a_real_partial_quorum(self) -> None:
        scoped = config(
            snapshot_path=str(self.root / "partial-quorum.json"),
            proof_profiles={
                "release": {
                    "required_categories": ["market", "language", "device"],
                    "minimum_categories": 2,
                },
            },
        )
        engine = ClosureEngine(scoped, self.ledger)
        source = Event(
            source="linear",
            id="linear:partial-quorum",
            title="Launch ready",
            action_required=True,
            occurred_at=NOW,
            metadata={
                "project": "Launch",
                "evidence_categories": ["market", "language"],
            },
        )
        output = engine.reconcile([source], NOW)
        obligation = self.ledger.get(output[0].metadata["obligation_id"])
        proof = next(gate for gate in obligation.gates if gate.name == "evidence")
        self.assertEqual(proof.state, "satisfied")

    def test_newer_source_regression_reopens_a_previously_satisfied_gate(self) -> None:
        ready = Event(
            source="linear",
            id="linear:gate-regression",
            title="Launch deployed, access granted and validated",
            action_required=True,
            occurred_at=NOW,
            metadata={"project": "Launch", "gate_status": {"code": "satisfied", "deployment": "satisfied", "access": "satisfied", "validation": "satisfied"}},
        )
        first = self.engine.reconcile([ready], NOW)[0]
        first_obligation = self.ledger.get(first.metadata["obligation_id"])
        self.assertEqual(next(gate for gate in first_obligation.gates if gate.name == "access").state, "satisfied")
        regressed = Event(
            source="linear",
            id="linear:gate-regression",
            title="Launch blocked, need access before go-live",
            action_required=True,
            occurred_at=NOW + timedelta(minutes=1),
            metadata={"project": "Launch", "gate_status": {"access": "blocked"}},
        )
        second = self.engine.reconcile([regressed], NOW + timedelta(minutes=1))[0]
        obligation = self.ledger.get(second.metadata["obligation_id"])
        self.assertEqual(next(gate for gate in obligation.gates if gate.name == "access").state, "blocked")

    def test_poll_time_changes_do_not_reopen_a_manually_closed_obligation(self) -> None:
        initial = Event(source="gmail", id="gmail:stable", title="Please confirm", action_required=True, occurred_at=NOW)
        output = self.engine.reconcile([initial], NOW)
        obligation_id = output[0].metadata["obligation_id"]
        self.ledger.transition(obligation_id, "closed", reason="operator", now=NOW + timedelta(minutes=1))
        repolled = Event(
            source="gmail",
            id="gmail:stable",
            title="Please confirm",
            action_required=True,
            occurred_at=NOW + timedelta(minutes=2),
            expires_at=NOW + timedelta(days=2),
        )
        self.assertEqual(self.engine.reconcile([repolled], NOW + timedelta(minutes=2)), [])

    def test_scheduled_non_actionable_meeting_does_not_create_an_obligation(self) -> None:
        scheduled = Event(
            source="calendar",
            id="calendar:routine",
            title="Routine sync",
            kind="meeting",
            action_required=False,
            occurred_at=NOW,
            metadata={"meeting_phase": "scheduled"},
        )
        self.assertEqual(self.engine.reconcile([scheduled], NOW), [])

    def test_scheduled_countdown_alone_does_not_invent_a_meeting_decision(self) -> None:
        scheduled = Event(
            source="calendar",
            id="calendar:routine-countdown",
            title="Routine sync starts in ten minutes",
            kind="meeting",
            action_required=True,
            occurred_at=NOW,
            metadata={"meeting_id": "routine-countdown", "meeting_phase": "scheduled"},
        )
        self.assertEqual(self.engine.reconcile([scheduled], NOW), [])

    def test_pre_meeting_context_surfaces_the_overdue_relationship_commitment(self) -> None:
        commitment = Event(
            source="gmail",
            id="gmail:partner-promise",
            title="I will send the proposal",
            action_required=True,
            occurred_at=NOW - timedelta(days=1),
            due_at=NOW - timedelta(hours=1),
            metadata={
                "direction": "outgoing",
                "thread_id": "partner-promise",
                "counterparty": "Partner",
                "relationship_key": "partner.example",
            },
        )
        meeting = Event(
            source="calendar",
            id="calendar:partner-review:before",
            title="PREP Partner review",
            kind="meeting",
            action_required=True,
            occurred_at=NOW,
            metadata={
                "meeting_id": "partner-review",
                "meeting_phase": "before",
                "relationship_key": "partner.example",
            },
        )
        output = self.engine.reconcile([commitment, meeting], NOW)
        meeting_event = next(event for event in output if event.metadata["profile"] == "meeting")
        self.assertIn("OVERDUE", meeting_event.title)
        meeting_obligation = self.ledger.get(meeting_event.metadata["obligation_id"])
        self.assertTrue(meeting_obligation.metadata["meeting_context_obligation_id"])

    def test_relationship_memory_captures_non_actionable_interactions_without_regression(self) -> None:
        recent = Event(
            source="gmail",
            id="gmail:relationship-recent",
            title="Partner: For your information",
            action_required=False,
            occurred_at=NOW,
            metadata={"counterparty": "Partner", "relationship_key": "partner.example"},
        )
        self.assertEqual(self.engine.reconcile([recent], NOW), [])
        relationship = self.ledger.relationship("partner.example")
        self.assertEqual(relationship.last_interaction_at, NOW)
        older = Event(
            source="gmail",
            id="gmail:relationship-old",
            title="Partner: Older note",
            action_required=False,
            occurred_at=NOW - timedelta(days=2),
            metadata={"counterparty": "Partner", "relationship_key": "partner.example"},
        )
        self.engine.reconcile([older], NOW + timedelta(minutes=1))
        self.assertEqual(self.ledger.relationship("partner.example").last_interaction_at, NOW)

    def test_project_rollup_is_not_mistaken_for_release_evidence(self) -> None:
        output = self.engine.reconcile([
            Event(
                source="linear",
                id="linear:rollup",
                title="Project risk summary",
                kind="blocker",
                action_required=True,
                occurred_at=NOW,
                metadata={"rollup": True, "project": "Core"},
            )
        ], NOW)
        obligation = self.ledger.get(output[0].metadata["obligation_id"])
        self.assertEqual(obligation.metadata["profile"], "commitment")
        self.assertNotIn("evidence", {gate.name for gate in obligation.gates})

    def test_distinct_email_threads_do_not_collapse_into_one_customer_obligation(self) -> None:
        events = [
            Event(
                source="gmail",
                id=f"gmail:{thread}",
                title=f"Please confirm item {thread}",
                action_required=True,
                occurred_at=NOW,
                metadata={"thread_id": thread, "counterparty": "Partner", "relationship_key": "partner.example"},
            )
            for thread in ("one", "two")
        ]
        output = self.engine.reconcile(events, NOW)
        self.assertEqual(len(output), 2)

    def test_meeting_identity_wins_over_a_shared_attendee_domain(self) -> None:
        before = Event(
            source="calendar",
            id="calendar:domain:before",
            title="PREP Partner review",
            kind="meeting",
            action_required=True,
            occurred_at=NOW,
            metadata={"meeting_id": "review-1", "meeting_phase": "before", "relationship_key": "partner.example"},
        )
        first = self.engine.reconcile([before], NOW)
        after = Event(
            source="calendar",
            id="calendar:domain:after",
            title="FOLLOW-UP Partner review",
            kind="meeting",
            action_required=True,
            occurred_at=NOW + timedelta(hours=1),
            metadata={"meeting_id": "review-1", "meeting_phase": "after", "relationship_key": "partner.example"},
        )
        second = self.engine.reconcile([after], NOW + timedelta(hours=1))
        self.assertEqual(first[0].metadata["obligation_id"], second[0].metadata["obligation_id"])
        self.assertEqual(len(self.ledger.list(active_only=True)), 1)

    def test_unchanged_repoll_does_not_append_audit_or_move_interaction_time(self) -> None:
        original = Event(
            source="gmail",
            id="gmail:stable-audit",
            title="Please confirm the proposal",
            action_required=True,
            occurred_at=NOW,
            metadata={"thread_id": "stable-audit"},
        )
        first = self.engine.reconcile([original], NOW)[0]
        obligation_id = first.metadata["obligation_id"]
        audit_count = len(self.ledger.audit(obligation_id))
        repolled = Event(
            source="gmail",
            id=original.id,
            title=original.title,
            action_required=True,
            occurred_at=NOW + timedelta(minutes=5),
            expires_at=NOW + timedelta(hours=2),
            metadata=original.metadata,
        )
        self.engine.reconcile([repolled], NOW + timedelta(minutes=5))
        obligation = self.ledger.get(obligation_id)
        self.assertEqual(obligation.last_interaction_at, NOW)
        self.assertEqual(len(self.ledger.audit(obligation_id)), audit_count)

    def test_capacity_governance_is_idempotent_and_restores_base_title(self) -> None:
        due = NOW + timedelta(days=1)
        task = Event(
            source="linear",
            id="linear:capacity-idempotent",
            title="Ship partner export",
            action_required=True,
            due_at=due,
            occurred_at=NOW,
            metadata={"assignee": "Alex"},
        )
        absence = Event(
            source="calendar",
            id="calendar:capacity-idempotent",
            title="Alex vacation",
            occurred_at=NOW,
            expires_at=due + timedelta(days=1),
            metadata={
                "availability": "unavailable",
                "person": "Alex",
                "start_at": NOW,
                "end_at": due + timedelta(days=1),
            },
        )
        first = self.engine.reconcile([task, absence], NOW)[0]
        obligation_id = first.metadata["obligation_id"]
        governed = self.ledger.get(obligation_id)
        first_priority = governed.priority
        first_audit_count = len(self.ledger.audit(obligation_id))
        self.engine.reconcile([absence], NOW + timedelta(minutes=5))
        unchanged = self.ledger.get(obligation_id)
        self.assertEqual(unchanged.priority, first_priority)
        self.assertEqual(len(self.ledger.audit(obligation_id)), first_audit_count)
        self.engine.reconcile([], due + timedelta(days=2))
        restored = self.ledger.get(obligation_id)
        self.assertEqual(restored.title, restored.metadata["base_title"])
        self.assertEqual(restored.priority, restored.metadata["base_priority"])

    def test_evidence_expiry_is_anchored_to_the_source_observation(self) -> None:
        source = Event(
            source="posthog",
            id="posthog:stable-proof",
            title="Analytics validated",
            action_required=True,
            occurred_at=NOW,
            metadata={"project": "Launch", "evidence_categories": ["analytics"]},
        )
        first = self.engine.reconcile([source], NOW)[0]
        obligation_id = first.metadata["obligation_id"]
        evidence = self.ledger.get(obligation_id).evidence
        self.assertEqual(evidence[0].expires_at, NOW + timedelta(hours=168))
        self.engine.reconcile([source], NOW + timedelta(hours=169))
        self.assertEqual(self.ledger.get(obligation_id).evidence, ())

    def test_proof_expiry_is_reconciled_when_the_source_disappears(self) -> None:
        scoped = config(
            snapshot_path=str(self.root / "expiring-proof.json"),
            proof_profiles={
                "release": {
                    "required_categories": ["deployment"],
                    "minimum_categories": 1,
                },
            },
        )
        engine = ClosureEngine(scoped, self.ledger)
        source = Event(
            source="deployment",
            id="deployment:expiring-proof",
            title="Launch deployed",
            action_required=True,
            occurred_at=NOW,
            metadata={
                "project": "Launch",
                "deployment_status": "success",
                "gate_status": {
                    "code": "satisfied",
                    "access": "satisfied",
                    "validation": "satisfied",
                },
                "evidence_categories": ["deployment"],
            },
        )
        first = engine.reconcile([source], NOW)[0]
        obligation_id = first.metadata["obligation_id"]
        proof = next(
            gate for gate in self.ledger.get(obligation_id).gates
            if gate.name == "evidence"
        )
        self.assertEqual(proof.state, "satisfied")
        output = engine.reconcile([], NOW + timedelta(hours=169))
        obligation = self.ledger.get(obligation_id)
        self.assertEqual(obligation.evidence, ())
        proof = next(gate for gate in obligation.gates if gate.name == "evidence")
        self.assertNotEqual(proof.state, "satisfied")
        self.assertEqual(output[0].metadata["obligation_id"], obligation_id)

    def test_schema_one_ledger_migrates_to_the_audited_schema(self) -> None:
        path = self.root / "version-one.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version = 1")
        connection.close()
        ledger = ObligationLedger(path)
        try:
            columns = ledger._connection.execute("PRAGMA table_info(audit_entries)").fetchall()
            version = ledger._connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 2)
            self.assertIn("payload_hash", {row[1] for row in columns})
        finally:
            ledger.close()


if __name__ == "__main__":
    unittest.main()
