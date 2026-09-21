"""Structural tests over the shipped n8n workflow JSONs.

These prove the reliability story the repo sells: every workflow is a valid
n8n shape, every one has error handling AND retry config, and none exists
without a failure path. If a future edit drops a retry flag or orphans a
dead-letter branch, this file goes red.
"""

import pytest

from n8n_reliability.validator import (
    check_error_handling,
    check_failure_path,
    check_retry_config,
    validate_schema,
    validate_workflow,
)


def _node(wf, name):
    for node in wf["nodes"]:
        if node["name"] == name:
            return node
    raise AssertionError(f"node {name!r} not found in {wf['name']!r}")


def _targets(wf, source_name):
    """All node names reachable from a node's outputs (every output index)."""
    conns = wf["connections"].get(source_name, {}).get("main", [])
    return [c["node"] for output in conns for c in output]


# --- the files themselves --------------------------------------------------


def test_all_three_workflow_files_exist(workflow_paths):
    import os

    assert len(workflow_paths) == 3
    for name, path in workflow_paths.items():
        assert os.path.isfile(path), f"missing workflow file: {name}"


def test_all_workflows_parse_as_json(workflow_paths):
    import json

    for name, path in workflow_paths.items():
        with open(path) as f:
            data = json.load(f)
        assert isinstance(data, dict), f"{name} is not a JSON object"


def test_all_workflows_pass_schema_validation(loaded_workflows):
    for name, wf in loaded_workflows.items():
        issues = validate_schema(wf)
        assert issues == [], f"{name}: {[i.message for i in issues]}"


def test_all_workflows_are_reliable(loaded_workflows):
    for name, wf in loaded_workflows.items():
        report = validate_workflow(wf)
        assert report.is_reliable, (
            f"{name} is not reliable: "
            f"{[i.message for i in report.error_handling_issues]} "
            f"{[i.message for i in report.retry_issues]} "
            f"{[i.message for i in report.failure_path_issues]}"
        )


def test_every_workflow_has_error_handling(loaded_workflows):
    for name, wf in loaded_workflows.items():
        assert check_error_handling(wf) == [], f"{name} has no error handling"


def test_every_workflow_has_retry_config(loaded_workflows):
    for name, wf in loaded_workflows.items():
        assert check_retry_config(wf) == [], f"{name} is missing retry config"


def test_no_workflow_without_a_failure_path(loaded_workflows):
    for name, wf in loaded_workflows.items():
        assert check_failure_path(wf) == [], f"{name} has no failure path"


def test_every_connection_target_exists(loaded_workflows):
    for name, wf in loaded_workflows.items():
        known = {n["name"] for n in wf["nodes"]}
        for source, targets in (
            (s, _targets(wf, s)) for s in wf["connections"]
        ):
            assert source in known, f"{name}: connection from unknown node {source!r}"
            for target in targets:
                assert target in known, f"{name}: {source!r} -> unknown node {target!r}"


# --- lead intake pipeline --------------------------------------------------


def test_lead_intake_starts_with_webhook_trigger(loaded_workflows):
    wf = loaded_workflows["lead-intake-pipeline.json"]
    triggers = [
        n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"
    ]
    assert len(triggers) == 1
    assert triggers[0]["name"] == "Lead Webhook"


def test_lead_intake_crm_write_has_retry(loaded_workflows):
    wf = loaded_workflows["lead-intake-pipeline.json"]
    crm = _node(wf, "Write to CRM")
    assert crm["retryOnFail"] is True
    assert crm["maxTries"] >= 2
    assert crm["waitBetweenTries"] > 0


def test_lead_intake_crm_failure_routes_to_dead_letter(loaded_workflows):
    wf = loaded_workflows["lead-intake-pipeline.json"]
    crm = _node(wf, "Write to CRM")
    assert crm.get("onError") == "continueErrorOutput"
    # The error output (second output array) must route to the dead-letter node.
    error_targets = [
        c["node"]
        for c in wf["connections"]["Write to CRM"]["main"][1]
    ]
    assert "Dead-letter Lead" in error_targets


def test_lead_intake_dead_letter_alerts_a_human(loaded_workflows):
    wf = loaded_workflows["lead-intake-pipeline.json"]
    assert "Alert — Lead Failed" in _targets(wf, "Dead-letter Lead")


def test_lead_intake_rejects_unqualified_leads(loaded_workflows):
    wf = loaded_workflows["lead-intake-pipeline.json"]
    qualify_outputs = wf["connections"]["Qualify Lead"]["main"]
    assert len(qualify_outputs) == 2  # qualified branch + decline branch
    declined = [c["node"] for c in qualify_outputs[1]]
    assert "Polite Decline" in declined


# --- nightly CRM sync ------------------------------------------------------


def test_nightly_sync_has_schedule_trigger(loaded_workflows):
    wf = loaded_workflows["nightly-crm-sync.json"]
    triggers = [
        n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.scheduleTrigger"
    ]
    assert len(triggers) == 1
    assert triggers[0]["name"] == "Midnight Schedule"


def test_nightly_sync_names_an_error_workflow(loaded_workflows):
    wf = loaded_workflows["nightly-crm-sync.json"]
    assert wf["settings"].get("errorWorkflow") == "nightly-sync-error-handler"


def test_nightly_sync_record_write_has_retry_and_error_branch(loaded_workflows):
    wf = loaded_workflows["nightly-crm-sync.json"]
    sync = _node(wf, "Sync Record to CRM")
    assert sync["retryOnFail"] is True
    assert sync["maxTries"] >= 3
    assert sync.get("onError") == "continueErrorOutput"
    error_targets = [c["node"] for c in wf["connections"]["Sync Record to CRM"]["main"][1]]
    assert "Dead-letter Record" in error_targets


def test_nightly_sync_dead_letters_stay_visible(loaded_workflows):
    wf = loaded_workflows["nightly-crm-sync.json"]
    # Dead-lettered records must surface in the run summary, not vanish.
    assert "Sync Summary" in _targets(wf, "Dead-letter Record")


# --- dead-letter reprocessor -----------------------------------------------


def test_dlq_reprocessor_runs_on_a_schedule(loaded_workflows):
    wf = loaded_workflows["dead-letter-reprocessor.json"]
    triggers = [
        n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.scheduleTrigger"
    ]
    assert len(triggers) == 1
    assert triggers[0]["name"] == "Every 15 Minutes"


def test_dlq_reprocessor_has_requeue_and_quarantine_paths(loaded_workflows):
    wf = loaded_workflows["dead-letter-reprocessor.json"]
    branch = wf["connections"]["Attempts Remaining?"]["main"]
    assert len(branch) == 2
    assert "Reprocess Item" in [c["node"] for c in branch[0]]
    assert "Quarantine Item" in [c["node"] for c in branch[1]]


def test_dlq_reprocessor_quarantine_pages_a_human(loaded_workflows):
    wf = loaded_workflows["dead-letter-reprocessor.json"]
    assert "Alert — Quarantined" in _targets(wf, "Quarantine Item")


def test_dlq_reprocessor_failed_reprocess_goes_to_quarantine(loaded_workflows):
    wf = loaded_workflows["dead-letter-reprocessor.json"]
    reprocess = _node(wf, "Reprocess Item")
    assert reprocess["retryOnFail"] is True
    assert reprocess.get("onError") == "continueErrorOutput"
    # Even the reprocessor's own failure must not drop the item.
    error_targets = [c["node"] for c in wf["connections"]["Reprocess Item"]["main"][1]]
    assert "Quarantine Item" in error_targets


def test_dlq_reprocessor_names_an_error_workflow(loaded_workflows):
    wf = loaded_workflows["dead-letter-reprocessor.json"]
    assert wf["settings"].get("errorWorkflow") == "dlq-reprocessor-error-handler"
