"""Structural + reliability validation for n8n workflow JSON exports.

This is the "Tuesday at 2am" checklist as code. A workflow is *reliable*
only if it passes all four checks:

1. **Schema** — valid n8n shape: nodes with id/name/type/parameters/position,
   connections that reference real nodes, a settings object.
2. **Error handling** — the workflow names an ``errorWorkflow``, contains an
   Error Trigger node, or routes failures via ``onError``. Something must
   catch the 2am failure; the default (stop the workflow, lose the data)
   is not acceptable.
3. **Retry config** — every node that calls an external system must set
   ``retryOnFail`` with explicit ``maxTries >= 2`` and ``waitBetweenTries``.
   Explicit, not defaulted: retry settings you cannot see are settings you
   cannot audit.
4. **Failure path** — there must be a wired destination for failures: an
   error workflow, an error-output branch, or a dead-letter / alert /
   quarantine node that something actually routes to. An unwired node with
   "dead letter" in its name is decoration, not a failure path.

``validate_workflow`` runs everything and returns a :class:`WorkflowReport`;
``report.is_reliable`` is the single boolean the CI gate checks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Node types that call external systems and therefore must retry.
#: Triggers (webhook, scheduleTrigger) and pure-logic nodes (if, set,
#: code, merge, ...) are exempt — they never touch the network.
NETWORK_NODE_TYPES = frozenset(
    {
        "n8n-nodes-base.httpRequest",
        "n8n-nodes-base.slack",
        "n8n-nodes-base.sendEmail",
        "n8n-nodes-base.smtp",
        "n8n-nodes-base.hubspot",
        "n8n-nodes-base.salesforce",
        "n8n-nodes-base.notion",
        "n8n-nodes-base.googleSheets",
        "n8n-nodes-base.airtable",
        "n8n-nodes-base.postgres",
        "n8n-nodes-base.mysql",
        "n8n-nodes-base.openAi",
    }
)

#: n8n ``onError`` modes that keep the workflow alive on node failure.
ERROR_OUTPUT_MODES = frozenset({"continueErrorOutput", "continueRegularOutput"})

#: Hints that a node name describes a failure sink (dead-letter, alert, ...).
FAILURE_SINK_HINTS = (
    "dead",
    "letter",
    "quarantine",
    "alert",
    "pagerduty",
    "oncall",
    "error",
)

_ERROR_TRIGGER_TYPE = "n8n-nodes-base.errorTrigger"


@dataclass
class Issue:
    """One failed check."""

    check: str
    message: str
    node: str | None = None


@dataclass
class WorkflowReport:
    """The verdict for one workflow: the issues found per check."""

    name: str
    schema_issues: list[Issue] = field(default_factory=list)
    error_handling_issues: list[Issue] = field(default_factory=list)
    retry_issues: list[Issue] = field(default_factory=list)
    failure_path_issues: list[Issue] = field(default_factory=list)

    @property
    def is_reliable(self) -> bool:
        """True only when every check is clean."""
        return not (
            self.schema_issues
            or self.error_handling_issues
            or self.retry_issues
            or self.failure_path_issues
        )


def load_workflow(path: str | Path) -> dict[str, Any]:
    """Load an n8n workflow JSON export.

    Raises:
        FileNotFoundError: If the path does not exist.
        ValueError: If the file is not valid JSON.
    """
    raw_path = Path(path)
    if not raw_path.is_file():
        raise FileNotFoundError(f"workflow file not found: {raw_path}")
    try:
        return json.loads(raw_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {raw_path}: {exc}") from exc


def _nodes(workflow: dict) -> list[dict]:
    nodes = workflow.get("nodes")
    return nodes if isinstance(nodes, list) else []


def _node_map(workflow: dict) -> dict[str, dict]:
    return {
        node["name"]: node
        for node in _nodes(workflow)
        if isinstance(node, dict) and isinstance(node.get("name"), str)
    }


def _connections(workflow: dict) -> dict[str, Any]:
    conns = workflow.get("connections")
    return conns if isinstance(conns, dict) else {}


def _targets(workflow: dict, source: str) -> list[str]:
    """Every node name reachable from any output of ``source``."""
    payload = _connections(workflow).get(source)
    outputs = payload.get("main", []) if isinstance(payload, dict) else []
    names: list[str] = []
    for output in outputs if isinstance(outputs, list) else []:
        for conn in output if isinstance(output, list) else []:
            if isinstance(conn, dict) and isinstance(conn.get("node"), str):
                names.append(conn["node"])
    return names


def _wired_targets(workflow: dict) -> set[str]:
    """All node names that have at least one incoming connection."""
    targets: set[str] = set()
    for source in _connections(workflow):
        targets.update(_targets(workflow, source))
    return targets


# --- check 1: schema -------------------------------------------------------


def validate_schema(workflow: dict[str, Any]) -> list[Issue]:
    """Check the workflow has a valid n8n shape."""
    issues: list[Issue] = []
    if not isinstance(workflow, dict):
        return [Issue("schema", "workflow must be a JSON object")]

    name = workflow.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append(Issue("schema", "workflow must have a non-empty name"))

    nodes = workflow.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        issues.append(Issue("schema", "workflow must have a non-empty nodes list"))
        return issues

    seen_names: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            issues.append(Issue("schema", "every node must be an object"))
            continue
        node_name = node.get("name")
        for key in ("id", "name", "type"):
            value = node.get(key)
            if not isinstance(value, str) or not value.strip():
                issues.append(
                    Issue(
                        "schema",
                        f"node is missing a non-empty {key!r}",
                        node=node_name if isinstance(node_name, str) else None,
                    )
                )
        params = node.get("parameters")
        if not isinstance(params, dict):
            issues.append(
                Issue(
                    "schema",
                    "node parameters must be an object",
                    node=node_name if isinstance(node_name, str) else None,
                )
            )
        position = node.get("position")
        if (
            not isinstance(position, list)
            or len(position) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in position)
        ):
            issues.append(
                Issue(
                    "schema",
                    "node position must be [x, y]",
                    node=node_name if isinstance(node_name, str) else None,
                )
            )
        if isinstance(node_name, str):
            if node_name in seen_names:
                issues.append(
                    Issue("schema", f"duplicate node name {node_name!r}", node=node_name)
                )
            seen_names.add(node_name)

    connections = workflow.get("connections")
    if not isinstance(connections, dict):
        issues.append(Issue("schema", "workflow connections must be an object"))
    else:
        for source, payload in connections.items():
            if source not in seen_names:
                issues.append(
                    Issue("schema", f"connection from unknown node {source!r}")
                )
            outputs = payload.get("main") if isinstance(payload, dict) else None
            if not isinstance(outputs, list):
                issues.append(
                    Issue("schema", f"connection {source!r} must have a main output list")
                )
                continue
            for output in outputs:
                if not isinstance(output, list):
                    issues.append(
                        Issue(
                            "schema",
                            f"connection {source!r} outputs must be lists",
                        )
                    )
                    continue
                for conn in output:
                    target = conn.get("node") if isinstance(conn, dict) else None
                    if not isinstance(target, str) or target not in seen_names:
                        issues.append(
                            Issue(
                                "schema",
                                f"connection {source!r} targets unknown node {target!r}",
                            )
                        )

    settings = workflow.get("settings")
    if settings is not None and not isinstance(settings, dict):
        issues.append(Issue("schema", "workflow settings must be an object"))

    return issues


# --- check 2: error handling ------------------------------------------------


def check_error_handling(workflow: dict[str, Any]) -> list[Issue]:
    """Require a mechanism that catches node failures.

    Passes if the workflow names an ``errorWorkflow``, contains an Error
    Trigger node, or any node routes failures onward via ``onError``.
    """
    settings = workflow.get("settings") or {}
    if isinstance(settings.get("errorWorkflow"), str) and settings["errorWorkflow"].strip():
        return []
    for node in _nodes(workflow):
        if not isinstance(node, dict):
            continue
        if node.get("type") == _ERROR_TRIGGER_TYPE:
            return []
        if node.get("onError") in ERROR_OUTPUT_MODES:
            return []
    return [
        Issue(
            "error-handling",
            "no error handling: set settings.errorWorkflow, add an Error Trigger "
            "node, or set onError to continueErrorOutput on failing nodes",
        )
    ]


# --- check 3: retry config --------------------------------------------------


def check_retry_config(workflow: dict[str, Any]) -> list[Issue]:
    """Require explicit retry settings on every network-touching node."""
    issues: list[Issue] = []
    for node in _nodes(workflow):
        if not isinstance(node, dict):
            continue
        if node.get("type") not in NETWORK_NODE_TYPES:
            continue
        name = node.get("name")
        if node.get("retryOnFail") is not True:
            issues.append(
                Issue(
                    "retry",
                    "network node must set retryOnFail: true",
                    node=name,
                )
            )
        max_tries = node.get("maxTries", 0)
        if not isinstance(max_tries, int) or isinstance(max_tries, bool) or max_tries < 2:
            issues.append(
                Issue(
                    "retry",
                    "network node must set maxTries >= 2",
                    node=name,
                )
            )
        wait = node.get("waitBetweenTries", 0)
        if not isinstance(wait, (int, float)) or isinstance(wait, bool) or wait <= 0:
            issues.append(
                Issue(
                    "retry",
                    "network node must set waitBetweenTries > 0",
                    node=name,
                )
            )
    return issues


# --- check 4: failure path --------------------------------------------------


def check_failure_path(workflow: dict[str, Any]) -> list[Issue]:
    """Require a wired destination for failures.

    Passes if the workflow names an ``errorWorkflow``, contains an Error
    Trigger node, wires an ``onError`` error-output branch to a downstream
    node, or routes to a dead-letter / alert / quarantine node. The sink
    must be wired — a node nobody routes to is decoration, not a path.
    """
    settings = workflow.get("settings") or {}
    if isinstance(settings.get("errorWorkflow"), str) and settings["errorWorkflow"].strip():
        return []
    wired = _wired_targets(workflow)
    for node in _nodes(workflow):
        if not isinstance(node, dict):
            continue
        name = node.get("name")
        if not isinstance(name, str):
            continue
        if node.get("type") == _ERROR_TRIGGER_TYPE and name in wired:
            return []
        lowered = name.lower()
        if any(hint in lowered for hint in FAILURE_SINK_HINTS) and name in wired:
            return []
        if node.get("onError") in ERROR_OUTPUT_MODES and _targets(workflow, name):
            return []
    return [
        Issue(
            "failure-path",
            "no failure path: wire failures to a dead-letter, alert, or "
            "quarantine node, or set settings.errorWorkflow",
        )
    ]


# --- full report ------------------------------------------------------------


def validate_workflow(workflow: dict[str, Any]) -> WorkflowReport:
    """Run all four checks and return the verdict."""
    name = workflow.get("name") if isinstance(workflow, dict) else None
    return WorkflowReport(
        name=name if isinstance(name, str) else "<unnamed>",
        schema_issues=validate_schema(workflow),
        error_handling_issues=check_error_handling(workflow),
        retry_issues=check_retry_config(workflow),
        failure_path_issues=check_failure_path(workflow),
    )


def validate_workflow_file(path: str | Path) -> WorkflowReport:
    """Load a workflow JSON file and validate it."""
    return validate_workflow(load_workflow(path))
