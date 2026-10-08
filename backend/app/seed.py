"""Deterministic demo data.

``seed`` deliberately creates tickets whose SLA position is *known*: 已超时,
临界, 健康, 依赖节假日.  A portfolio demo that only contains happy-path tickets
proves nothing about the engine; these fixtures exercise every risk band and the
holiday calendar in a single command.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from .domain.ticket import Ticket
from .domain.values import Priority, Status, allowed_transitions

CN = timezone(timedelta(hours=8))

#: Ticket specs, oldest first.  ``created_before`` is relative to the seed clock.
SEED_TICKETS: list[dict[str, Any]] = [
    {
        "id": "TCK-2025-0001",
        "title": "核心支付网关 502，全站下单失败",
        "body": "9:12 起支付回调全部 502，监控显示网关实例全部重启中，已影响全部用户下单。",
        "reporter": "监控告警",
        "channel": "alert",
        "tags": ["incident"],
        "labels": {"env": "prod", "service": "payment-gateway"},
        "created_before": {"hours": 26},
        "post": {"status": "in_progress", "assignee": "sre-张伟"},
    },
    {
        "id": "TCK-2025-0002",
        "title": "结算对账金额不一致，昨日退款少记 3 笔",
        "body": "财务反馈昨日退款流水与渠道账单差 3 笔，涉及金额 4,280 元，需要核对。",
        "reporter": "财务-李静",
        "channel": "payments",
        "tags": ["payments", "reconcile"],
        "labels": {"env": "prod", "tier": "vip"},
        "created_before": {"hours": 9},
        "post": {"status": "in_progress", "assignee": "pay-刘洋"},
    },
    {
        "id": "TCK-2025-0003",
        "title": "移动端首页加载卡顿，接口超时率升高",
        "body": "首页接口 P95 从 180ms 涨到 2.4s，部分用户白屏，疑似缓存穿透。",
        "reporter": "客服-王凯",
        "channel": "web",
        "tags": ["performance"],
        "labels": {"env": "prod", "platform": "mobile"},
        "created_before": {"hours": 5},
        "post": {"status": "triaged"},
    },
    {
        "id": "TCK-2025-0004",
        "title": "用户反馈导出报表缺少字段，无法对账",
        "body": "导出的 CSV 里少了『手续费』列，财务无法完成月结。",
        "reporter": "运营-张敏",
        "channel": "web",
        "tags": ["defect"],
        "labels": {"env": "prod", "module": "report"},
        "created_before": {"hours": 3},
        "post": {"status": "blocked", "assignee": "app-孙浩"},
    },
    {
        "id": "TCK-2025-0005",
        "title": "疑似越权访问：普通账号可读取他人订单详情",
        "body": "安全扫描发现 /api/orders/{id} 未校验归属，可越权读取任意订单，疑似数据泄露风险。",
        "reporter": "安全扫描",
        "channel": "alert",
        "tags": ["security"],
        "labels": {"env": "prod", "severity": "high"},
        "created_before": {"hours": 2},
        "post": {"status": "in_progress", "assignee": "sec-周琳"},
    },
    {
        "id": "TCK-2025-0006",
        "title": "请问如何申请沙箱环境的开放接口权限？",
        "body": "我们想接入开放平台接口，需要沙箱权限和一对测试密钥，流程是怎样的？",
        "reporter": "客户-星海科技",
        "channel": "email",
        "tags": ["request"],
        "labels": {"tier": "vip"},
        "created_before": {"hours": 1},
        "post": {"status": "triaged"},
    },
    {
        "id": "TCK-2025-0007",
        "title": "报表导出偶发 500 异常",
        "body": "大报表导出时偶发 500，日志里有 NullPointerException，频率约每天两次。",
        "reporter": "客服-赵磊",
        "channel": "web",
        "tags": ["defect"],
        "labels": {"env": "prod"},
        "created_before": {"minutes": 40},
        "post": {"status": "triaged", "assignee": "app-孙浩"},
    },
    {
        "id": "TCK-2025-0008",
        "title": "活动页文案配置后又变回去了",
        "body": "刚配置的活动文案保存成功，但刷新后恢复原值，怀疑是 CDN 缓存。",
        "reporter": "运营-陈曦",
        "channel": "web",
        "tags": ["defect"],
        "labels": {"env": "prod"},
        "created_before": {"minutes": 12},
        "post": {
            "status": "resolved",
            "assignee": "app-孙浩",
            # Resolved four minutes ago, so `closed_at` (stamped by the
            # transition itself) lands just inside the SLA target.
            "close_before": {"minutes": 4},
        },
    },
]


def seed_payloads(now: datetime) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return ``(create_payload, post_create_updates)`` pairs for the given clock.

    ``post_create_updates`` may carry a ``close_before`` offset (a pseudo-field,
    not a ticket column): the caller advances its clock by that much before
    resolving, which is how the seed controls ``closed_at`` without writing it
    directly — ``closed_at`` is owned by the state machine.
    """
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for spec in SEED_TICKETS:
        created = now - _delta(spec.get("created_before", {}))
        payload = {
            "id": spec["id"],
            "title": spec["title"],
            "body": spec["body"],
            "reporter": spec["reporter"],
            "channel": spec["channel"],
            "tags": list(spec["tags"]),
            "labels": dict(spec["labels"]),
            "created_at": created.isoformat(),
        }
        out.append((payload, dict(spec.get("post") or {})))
    return out


def close_moment(now: datetime, post: dict[str, Any]) -> datetime:
    """Resolve ``close_before`` into the instant the ticket should be closed at."""
    spec = post.get("close_before") or {}
    return now - _delta(spec) if spec else now


def seed_tickets(now: datetime) -> list[Ticket]:
    """Convenience wrapper for tests that only need the raw entities."""
    tickets: list[Ticket] = []
    for payload, _post in seed_payloads(now):
        tickets.append(
            Ticket.create(
                ticket_id=payload["id"],
                title=payload["title"],
                body=payload["body"],
                reporter=payload["reporter"],
                channel=payload["channel"],
                tags=tuple(payload["tags"]),
                labels=payload["labels"],
                created_at=datetime.fromisoformat(payload["created_at"]),
            )
        )
    return tickets


def demo_created_at(now: datetime, hours: float) -> str:
    return (now - timedelta(hours=hours)).isoformat()


def sample_payloads() -> list[dict[str, Any]]:
    """Small, readable payloads used by tests and by ``/api/v1/policy/preview``."""
    return [
        {"title": "生产环境数据库连接池耗尽", "body": "全站不可用", "channel": "alert"},
        {"title": "退款到账延迟，用户投诉", "body": "支付退款 3 天未到账", "channel": "payments"},
        {"title": "咨询发票如何开具", "body": "需要增值税专用发票", "channel": "email"},
        {"title": "页面偶发白屏", "body": "部分用户打开页面白屏，刷新可恢复", "channel": "web"},
    ]


def priority_of(payload: dict[str, Any]) -> Priority:
    return Priority.parse(payload.get("priority", Priority.P3))


def transition_path(source: Status, target: Status) -> list[str]:
    """Shortest legal route from ``source`` to ``target``.

    Seeding has to respect the same state machine as production.  Hard-coding a
    jump like ``triaged -> resolved`` breaks the moment the workflow gains a
    required intermediate step (as it did here: nothing reaches CLOSED without
    passing through RESOLVED), so the seed asks the domain for the route instead
    of assuming one.
    """
    if source == target:
        return []
    queue: deque[tuple[Status, list[Status]]] = deque([(source, [])])
    seen: set[Status] = {source}
    while queue:
        current, path = queue.popleft()
        for nxt in sorted(allowed_transitions(current), key=lambda item: item.value):
            if nxt in seen:
                continue
            route = [*path, nxt]
            if nxt == target:
                return [step.value for step in route]
            seen.add(nxt)
            queue.append((nxt, route))
    raise ValueError(f"no legal transition path from {source.value} to {target.value}")


def _delta(spec: dict[str, int]) -> timedelta:
    return timedelta(
        days=spec.get("days", 0),
        hours=spec.get("hours", 0),
        minutes=spec.get("minutes", 0),
        seconds=spec.get("seconds", 0),
    )
