"""n8n_reliability — the reliability patterns behind the shipped n8n workflows.

Three small modules, one story: what keeps an automation alive at 2am.

- :mod:`n8n_reliability.retry` — exponential-backoff retry policy, the Python
  mirror of n8n's ``retryOnFail`` node setting.
- :mod:`n8n_reliability.dlq` — file-backed dead-letter queue: failed items
  are parked, requeued, or quarantined — never silently dropped.
- :mod:`n8n_reliability.validator` — structural + reliability checks for n8n
  workflow JSON exports. Every shipped workflow must pass it.
"""

from n8n_reliability.retry import RetryPolicy
from n8n_reliability.dlq import DeadLetterQueue, DlqEntry
from n8n_reliability.validator import (
    Issue,
    WorkflowReport,
    load_workflow,
    validate_schema,
    validate_workflow,
    validate_workflow_file,
)

__all__ = [
    "RetryPolicy",
    "DeadLetterQueue",
    "DlqEntry",
    "Issue",
    "WorkflowReport",
    "load_workflow",
    "validate_schema",
    "validate_workflow",
    "validate_workflow_file",
]
