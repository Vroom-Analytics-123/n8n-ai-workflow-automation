"""Unit tests for n8n_reliability.validator.

These use small synthetic workflow dicts so every rule can be tested in
isolation: happy paths, edge cases, and the exact failure modes the repo
exists to prevent (a workflow with no retry config, no error handling, or
no failure path must NOT validate). The shipped JSONs are covered
separately in test_workflows.py.
"""

import pytest

from n8n_reliability.validator import (
    check_error_handling,
    check_failure_path,
    check_retry_config,
    load_workflow,
    validate_schema,
    validate_workflow,
    validate_workflow_file,
)


def _http_node(name="Write to CRM", **overrides):
    node = {
        "id": f"id-{name}",
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "parameters": {"url": "https://crm.example.com"},
        "position": [460, 300],
        "retryOnFail": True,
        "maxTries": 3,
        "waitBetweenTries": 5000,
        "onError": "continueErrorOutput",
    }
    node.update(overrides)
    return node


def _reliable_workflow(**overrides):
    """A workflow that should pass every check."""
    wf = {
        "name": "Reliable",
        "nodes": [
            {
                "id": "t1",
                "name": "Trigger",
                "type": "n8n-nodes-base.scheduleTrigger",
                "parameters": {},
                "position": [240, 300],
            },
            _http_node(),
            {
                "id": "d1",
                "name": "Dead-letter queue",
                "type": "n8n-nodes-base.code",
                "parameters": {},
                "position": [680, 300],
            },
        ],
        "connections": {
            "Trigger": {"main": [[{"node": "Write to CRM", "type": "main", "index": 0}]]},
            "Write to CRM": {
                "main": [
                    [],
                    [{"node": "Dead-letter queue", "type": "main", "index": 0}],
                ]
            },
        },
        "settings": {},
    }
    wf.update(overrides)
    return wf


# --- schema ---------------------------------------------------------------


def test_schema_accepts_minimal_valid_workflow(minimal_workflow):
    assert validate_schema(minimal_workflow) == []


def test_schema_rejects_missing_name(minimal_workflow):
    del minimal_workflow["name"]
    issues = validate_schema(minimal_workflow)
    assert issues and any("name" in i.message.lower() for i in issues)


def test_schema_rejects_empty_nodes(minimal_workflow):
    minimal_workflow["nodes"] = []
    assert validate_schema(minimal_workflow) != []


def test_schema_rejects_node_missing_id(minimal_workflow):
    del minimal_workflow["nodes"][0]["id"]
    issues = validate_schema(minimal_workflow)
    assert any(i.node == "Start" for i in issues)


def test_schema_rejects_node_missing_type(minimal_workflow):
    del minimal_workflow["nodes"][1]["type"]
    issues = validate_schema(minimal_workflow)
    assert any(i.node == "Finish" for i in issues)


def test_schema_rejects_node_with_bad_position(minimal_workflow):
    minimal_workflow["nodes"][0]["position"] = [240]  # needs [x, y]
    assert validate_schema(minimal_workflow) != []


def test_schema_rejects_node_with_non_dict_parameters(minimal_workflow):
    minimal_workflow["nodes"][0]["parameters"] = "not-a-dict"
    assert validate_schema(minimal_workflow) != []


def test_schema_rejects_duplicate_node_names(minimal_workflow):
    minimal_workflow["nodes"][1]["name"] = "Start"
    issues = validate_schema(minimal_workflow)
    assert any("duplicate" in i.message.lower() for i in issues)


def test_schema_rejects_connection_from_unknown_node(minimal_workflow):
    minimal_workflow["connections"]["Ghost"] = {
        "main": [[{"node": "Finish", "type": "main", "index": 0}]]
    }
    assert validate_schema(minimal_workflow) != []


def test_schema_rejects_connection_to_unknown_node(minimal_workflow):
    minimal_workflow["connections"]["Start"] = {
        "main": [[{"node": "Nobody", "type": "main", "index": 0}]]
    }
    assert validate_schema(minimal_workflow) != []


def test_schema_rejects_non_dict_connections(minimal_workflow):
    minimal_workflow["connections"] = ["Start"]
    assert validate_schema(minimal_workflow) != []


def test_schema_rejects_non_dict_settings(minimal_workflow):
    minimal_workflow["settings"] = "oops"
    assert validate_schema(minimal_workflow) != []


def test_schema_rejects_non_dict_workflow():
    assert validate_schema(["not", "a", "dict"]) != []


# --- error handling -------------------------------------------------------


def test_error_handling_fails_with_no_mechanism(minimal_workflow):
    minimal_workflow["settings"] = {}  # no errorWorkflow, no onError anywhere
    issues = check_error_handling(minimal_workflow)
    assert issues != []
    assert all(i.check == "error-handling" for i in issues)


def test_error_handling_passes_with_error_workflow_setting(minimal_workflow):
    assert check_error_handling(minimal_workflow) == []


def test_error_handling_passes_with_error_trigger_node(minimal_workflow):
    minimal_workflow["settings"] = {}
    minimal_workflow["nodes"].append(
        {
            "id": "e1",
            "name": "On Error",
            "type": "n8n-nodes-base.errorTrigger",
            "parameters": {},
            "position": [240, 500],
        }
    )
    assert check_error_handling(minimal_workflow) == []


def test_error_handling_passes_with_continue_error_output(minimal_workflow):
    minimal_workflow["settings"] = {}
    minimal_workflow["nodes"][1]["onError"] = "continueErrorOutput"
    assert check_error_handling(minimal_workflow) == []


def test_error_handling_passes_with_continue_regular_output(minimal_workflow):
    minimal_workflow["settings"] = {}
    minimal_workflow["nodes"][1]["onError"] = "continueRegularOutput"
    assert check_error_handling(minimal_workflow) == []


# --- retry config ---------------------------------------------------------


def test_retry_passes_with_full_config():
    assert check_retry_config(_reliable_workflow()) == []


def test_retry_fails_when_http_node_lacks_retry_flag():
    wf = _reliable_workflow()
    wf["nodes"][1].pop("retryOnFail")
    issues = check_retry_config(wf)
    assert any(i.node == "Write to CRM" and "retryOnFail" in i.message for i in issues)


def test_retry_fails_when_retry_explicitly_disabled():
    wf = _reliable_workflow()
    wf["nodes"][1]["retryOnFail"] = False
    assert check_retry_config(wf) != []


def test_retry_fails_when_max_tries_too_low():
    wf = _reliable_workflow()
    wf["nodes"][1]["maxTries"] = 1
    issues = check_retry_config(wf)
    assert any("maxTries" in i.message for i in issues)


def test_retry_fails_when_wait_between_tries_missing():
    wf = _reliable_workflow()
    wf["nodes"][1].pop("waitBetweenTries")
    issues = check_retry_config(wf)
    assert any("waitBetweenTries" in i.message for i in issues)


def test_retry_ignores_trigger_and_logic_nodes():
    wf = _reliable_workflow()
    # Triggers / logic nodes never touch the network: no retry expected.
    for node in wf["nodes"]:
        if node["type"] not in ("n8n-nodes-base.httpRequest",):
            node.pop("retryOnFail", None)
            node.pop("maxTries", None)
            node.pop("waitBetweenTries", None)
    assert check_retry_config(wf) == []


def test_retry_flags_every_network_node_missing_config():
    wf = _reliable_workflow()
    wf["nodes"].append(_http_node(name="Second CRM Call", retryOnFail=False))
    issues = check_retry_config(wf)
    assert any(i.node == "Second CRM Call" for i in issues)
    assert not any(i.node == "Write to CRM" for i in issues)


def test_retry_covers_slack_action_nodes():
    wf = _reliable_workflow()
    wf["nodes"].append(
        {
            "id": "s1",
            "name": "Alert",
            "type": "n8n-nodes-base.slack",
            "parameters": {},
            "position": [900, 300],
        }
    )
    issues = check_retry_config(wf)
    assert any(i.node == "Alert" for i in issues)


# --- failure path ---------------------------------------------------------


def test_failure_path_fails_with_no_failure_path(minimal_workflow):
    minimal_workflow["settings"] = {}
    issues = check_failure_path(minimal_workflow)
    assert issues != []
    assert all(i.check == "failure-path" for i in issues)


def test_failure_path_passes_with_error_workflow_setting(minimal_workflow):
    assert check_failure_path(minimal_workflow) == []


def test_failure_path_passes_with_wired_dead_letter_node():
    assert check_failure_path(_reliable_workflow()) == []


def test_failure_path_rejects_unwired_dead_letter_node():
    wf = _reliable_workflow()
    # Dead-letter node exists but nothing routes to it: not a real failure path.
    wf["connections"]["Write to CRM"]["main"][1] = []
    assert check_failure_path(wf) != []


def test_failure_path_passes_with_wired_error_output_branch(minimal_workflow):
    minimal_workflow["settings"] = {}
    minimal_workflow["nodes"][0]["onError"] = "continueErrorOutput"
    minimal_workflow["nodes"].append(
        {
            "id": "h1",
            "name": "Handle Failure",
            "type": "n8n-nodes-base.code",
            "parameters": {},
            "position": [460, 500],
        }
    )
    minimal_workflow["connections"]["Start"]["main"].append(
        [{"node": "Handle Failure", "type": "main", "index": 0}]
    )
    assert check_failure_path(minimal_workflow) == []


# --- report / loading -----------------------------------------------------


def test_validate_workflow_report_is_reliable_when_clean():
    report = validate_workflow(_reliable_workflow())
    assert report.is_reliable is True
    assert report.name == "Reliable"


def test_validate_workflow_report_lists_every_failing_check():
    wf = _reliable_workflow()
    wf["settings"] = {}
    wf["nodes"][1].pop("retryOnFail")  # break retry
    wf["nodes"][1].pop("onError")  # break error handling
    wf["connections"]["Write to CRM"]["main"][1] = []  # break failure path
    wf["nodes"][2]["name"] = "Archive"  # no longer a sink hint
    report = validate_workflow(wf)
    assert report.is_reliable is False
    assert report.error_handling_issues != []
    assert report.retry_issues != []
    assert report.failure_path_issues != []


def test_validate_workflow_file_reads_from_disk(tmp_path):
    import json

    path = tmp_path / "wf.json"
    path.write_text(json.dumps(_reliable_workflow()))
    report = validate_workflow_file(str(path))
    assert report.is_reliable is True


def test_load_workflow_reads_json_file(tmp_path):
    import json

    path = tmp_path / "wf.json"
    path.write_text(json.dumps({"name": "X"}))
    assert load_workflow(str(path)) == {"name": "X"}


def test_load_workflow_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_workflow(str(tmp_path / "missing.json"))


def test_load_workflow_raises_on_invalid_json(tmp_path):
    path = tmp_path / "wf.json"
    path.write_text("{not valid json")
    with pytest.raises(ValueError, match="[Jj][Ss][Oo][Nn]"):
        load_workflow(str(path))
