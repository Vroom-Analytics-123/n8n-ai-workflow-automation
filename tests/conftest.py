"""Shared fixtures for the n8n reliability test suite.

Makes ``src`` importable (example repo, no packaging) and loads the shipped
workflow JSONs so the structural tests always run against the real files.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

WORKFLOWS_DIR = os.path.join(os.path.dirname(__file__), "..", "workflows")

EXPECTED_WORKFLOWS = [
    "lead-intake-pipeline.json",
    "nightly-crm-sync.json",
    "dead-letter-reprocessor.json",
]


@pytest.fixture
def workflows_dir() -> str:
    return WORKFLOWS_DIR


@pytest.fixture
def workflow_paths() -> dict:
    """Map of workflow filename -> absolute path for the shipped JSONs."""
    return {
        name: os.path.join(WORKFLOWS_DIR, name) for name in EXPECTED_WORKFLOWS
    }


@pytest.fixture
def loaded_workflows(workflow_paths) -> dict:
    """Map of workflow filename -> parsed workflow dict."""
    from n8n_reliability.validator import load_workflow

    return {name: load_workflow(path) for name, path in workflow_paths.items()}


@pytest.fixture
def minimal_workflow() -> dict:
    """A small but fully reliable workflow dict used as a test fixture base."""
    return {
        "name": "Minimal",
        "nodes": [
            {
                "id": "a1",
                "name": "Start",
                "type": "n8n-nodes-base.webhook",
                "parameters": {},
                "position": [240, 300],
            },
            {
                "id": "b2",
                "name": "Finish",
                "type": "n8n-nodes-base.set",
                "parameters": {},
                "position": [460, 300],
            },
        ],
        "connections": {
            "Start": {"main": [[{"node": "Finish", "type": "main", "index": 0}]]},
        },
        "settings": {"errorWorkflow": "error-handler"},
    }


def raw_json(text: str):
    """Helper: parse inline JSON in tests that need a file on disk."""
    return json.loads(text)
