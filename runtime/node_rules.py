"""Registry of role-specific necessary-condition checks.

Roles without an entry keep the shared contract checks only. ``DRAFT_RULES`` run in
the node graph before the Judge and feed the bounded expression-fix retry;
``HANDOFF_RULES`` run at handoff and may use Judge labels.
"""

from __future__ import annotations

from runtime.domain_checks import domain_rule_errors, draft_rule_errors

DRAFT_RULES = {"domain": draft_rule_errors}
HANDOFF_RULES = {"domain": domain_rule_errors}
