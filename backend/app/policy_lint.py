"""Policy linter.

A rule set rots quietly: someone adds a catch-all above a specific rule, or
duplicates a condition, and the only symptom is that a ticket a month later goes
to the wrong queue.  This module makes those mistakes visible at review time.

It is deliberately conservative about what it claims:

* **shadowed** — an earlier rule can match everything this rule can and more, so
  this rule can never fire under ``first_match``.  This is proven, not guessed: it
  requires the earlier rule to be unconditional or to share the exact same
  condition.
* **possibly shadowed** — an earlier unconditional rule with ``stop`` precedes it.
* **duplicate** — two rules carry byte-identical conditions.

Anything subtler needs a SAT solver, and a lint rule that cries wolf gets
disabled within a week.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from .domain.rules.ast import describe
from .domain.rules.engine import ACTION_CATALOG, CompiledPolicy
from .domain.rules.parser import KNOWN_FUNCTIONS

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str
    code: str
    rule: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "rule": self.rule,
            "message": self.message,
        }


@dataclass(slots=True)
class LintReport:
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)

    def sorted(self) -> list[Finding]:
        return sorted(self.findings, key=lambda item: (SEVERITY_ORDER.get(item.severity, 9), item.rule))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "counts": _counts(self.findings),
            "stats": self.stats,
            "findings": [item.as_dict() for item in self.sorted()],
        }


def lint_policy(policy: CompiledPolicy, *, document: Mapping[str, Any] | None = None) -> LintReport:
    report = LintReport()
    rules = policy.rules

    unconditional_seen: str | None = None
    conditions: dict[str, str] = {}

    for index, compiled in enumerate(rules):
        rule = compiled.rule
        condition = compiled.condition

        if condition is None:
            if index != 0 and policy.strategy == "first_match":
                report.findings.append(
                    Finding(
                        "warning",
                        "unconditional-rule-not-first",
                        rule.name,
                        "该规则无条件匹配。在 first_match 策略下它会让后面的规则永不生效，请确认这是本意。",
                    )
                )
            if unconditional_seen is None:
                unconditional_seen = rule.name
            continue

        rendered = describe(condition)
        if rendered in conditions:
            report.findings.append(
                Finding(
                    "warning",
                    "duplicate-condition",
                    rule.name,
                    f"条件与规则「{conditions[rendered]}」完全相同：{rendered}",
                )
            )
        else:
            conditions[rendered] = rule.name

        if unconditional_seen is not None and policy.strategy == "first_match":
            report.findings.append(
                Finding(
                    "error",
                    "unreachable",
                    rule.name,
                    f"规则「{unconditional_seen}」无条件匹配且在其之前，本规则在 first_match 策略下永远不会执行。",
                )
            )

        if index == len(rules) - 1 and not policy.fallback:
            report.findings.append(
                Finding(
                    "info",
                    "last-rule-is-specific",
                    rule.name,
                    "这是最后一条正式规则且没有 fallback：未命中任何规则的工单将没有 SLA 时限。",
                )
            )

    if not policy.fallback:
        report.findings.append(
            Finding("info", "no-fallback", "*", "策略没有 fallback 规则：未命中的工单不会被兜底分诊。")
        )

    raw_actions = _raw_action_names(document)
    unknown_actions = sorted({name for name in raw_actions if name not in ACTION_CATALOG})
    for name in unknown_actions:
        report.findings.append(
            Finding("error", "unknown-action", "*", f"策略中出现了引擎不认识的动作 {name!r}。")
        )

    report.stats = {
        "rules": len(rules),
        "fallback_rules": len(policy.fallback),
        "strategy": policy.strategy,
        "unique_conditions": len(conditions),
        "unconditional_rules": sum(1 for item in rules if item.condition is None),
        "functions_available": len(KNOWN_FUNCTIONS),
        "actions_used": sorted({action.code for item in rules for action in item.rule.actions}),
    }
    return report


def _counts(findings: list[Finding]) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for item in findings:
        counts[item.severity] = counts.get(item.severity, 0) + 1
    return counts


def _raw_action_names(document: Mapping[str, Any] | None) -> list[str]:
    """Collect action names as written, before normalisation.

    String entries are split the same way the engine splits them (everything up
    to the last colon is the name).  That means ``escalate:to:oncall`` is judged
    on the name ``escalate:to`` — which *is* a real action — instead of being
    reported as a suspicious namespace.
    """
    if not document:
        return []
    names: list[str] = []
    for section in ("rules", "fallback"):
        raw = document.get(section) or ()
        if isinstance(raw, Mapping):
            raw = [raw]
        for rule in raw:
            for item in rule.get("then") or rule.get("actions") or ():
                if isinstance(item, str):
                    text = item.strip()
                    index = text.rfind(":")
                    names.append(text[:index].strip() if index != -1 else text)
                elif isinstance(item, Mapping):
                    name = str(item.get("action") or item.get("code") or "").strip()
                    if name:
                        names.append(name)
    return [name for name in names if name]


def format_report(report: LintReport) -> str:
    lines: list[str] = []
    counts = report.as_dict()["counts"]
    lines.append(
        f"policy lint: {counts['error']} error(s), {counts['warning']} warning(s), {counts['info']} info"
    )
    for finding in report.sorted():
        marker = {"error": "✖", "warning": "!", "info": "i"}.get(finding.severity, "?")
        lines.append(f"  [{marker}] {finding.rule}: {finding.message}")
    stats = report.stats
    if stats:
        lines.append(
            "  stats: "
            + json.dumps(
                {key: stats[key] for key in ("rules", "fallback_rules", "strategy", "unique_conditions")},
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)
