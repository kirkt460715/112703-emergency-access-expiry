"""本地核验紧急查阅的临时资格，并处置提前结束与各端收回。

证明标识（identity 来源）只说明身份，不能代替批准。一次临时资格必须同时满足：

* 申请人就是该单的当班人，且最近一次完成的身份核验覆盖审批时点与当班窗口；
* 审批人独立于申请人、在审批时仍可批准，并留下审批时点；
* 资格绑定这一单、这一事项、这一区域和所列操作，不能转给接手其他事项的人；
* 有效窗口以 ``scheduled_end`` 与事态实际结束时间中较早者为准。

所有判断只使用本地合成资料，资料中没有真实账号或居民资料；所有时间都是
相对分钟，证明标识不是凭据。本模块不写回原始资料，处置结果可以作为新的
结束／回执记录另行保存（见 :func:`apply_plan`）。
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from records_view import load_sources

APPROVER_ROLES = frozenset({"supervisor", "approver", "manager", "duty_leader"})
SOURCE_NAMES = ("identity", "requests", "checks")


class VerificationError(ValueError):
    """资料、参数或目标记录不满足核验条件。"""


# --------------------------------------------------------------------------
# 基础校验
# --------------------------------------------------------------------------

def _is_minutes(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _minutes(value, field):
    if not _is_minutes(value):
        raise VerificationError(f"{field} 需要非负整数分钟")
    return value


def _text(value):
    return isinstance(value, str) and bool(value)


def _strings(value):
    return isinstance(value, list) and all(_text(item) for item in value)


def _source(sources, name):
    if name not in sources:
        raise VerificationError(f"缺少 {name} 来源")
    return sources[name]


def _dedupe(items):
    seen = {}
    for item in items:
        if item is not None:
            seen.setdefault(item, None)
    return list(seen)


def _find_ticket(requests, ticket_id):
    """在同一来源内按标识找到业务单；结束记录没有申请人，不会被当成业务单。"""
    for row in requests:
        if (row.get("id") == ticket_id and _text(row.get("actor"))
                and _text(row.get("case")) and _text(row.get("region"))):
            return row
    raise VerificationError("该记录不存在或不可核验")


# --------------------------------------------------------------------------
# 身份、审批人与请求资格
# --------------------------------------------------------------------------

def _verifications(identity, actor, not_after=None):
    rows = []
    for row in identity:
        if row.get("actor") != actor or not _is_minutes(row.get("at")):
            continue
        if not_after is not None and row["at"] > not_after:
            continue
        rows.append(row)
    return rows


def _latest_verification(identity, actor, not_after=None):
    rows = _verifications(identity, actor, not_after)
    if not rows:
        return None
    # 最近的完成时点；同一时点优先明确的强核验记录。
    return max(rows, key=lambda row: (row["at"], row.get("assurance") == "strong"))


def _requester_view(identity, actor, at):
    row = _latest_verification(identity, actor, not_after=at)
    if row is None:
        return {
            "actor": actor,
            "last_completed_verification": None,
            "verified_at": None,
            "assurance": None,
            "on_shift_until": None,
            "may_request_now": False,
            "may_request_reason": "no_completed_verification",
        }
    until = row.get("on_shift_until")
    if not _is_minutes(until):
        reason = "shift_window_unknown"
        allowed = False
    elif at > until:
        reason = "shift_ended"
        allowed = False
    elif at < row["at"]:
        reason = "verification_not_completed"
        allowed = False
    else:
        reason = "on_shift_with_completed_verification"
        allowed = True
    return {
        "actor": actor,
        "last_completed_verification": row["id"],
        "verified_at": row["at"],
        "assurance": row.get("assurance"),
        "on_shift_until": until if _is_minutes(until) else None,
        "may_request_now": allowed,
        "may_request_reason": reason,
    }


def _approver_view(identity, actor, at):
    """某审批人在 ``at`` 时点是否仍可批准。"""
    rows = [row for row in identity
            if row.get("actor") == actor and _text(row.get("role")) and _is_minutes(row.get("until"))]
    if not rows:
        return {
            "actor": actor,
            "known": False,
            "role": None,
            "until": None,
            "available_at": False,
            "reason": "approver_unknown",
        }
    row = max(rows, key=lambda item: item["until"])
    role_ok = row.get("role") in APPROVER_ROLES
    available = role_ok and row["until"] >= at
    return {
        "actor": actor,
        "known": True,
        "role": row.get("role"),
        "until": row["until"],
        "available_at": available,
        "reason": "available" if available else "approver_unavailable_or_unauthorized",
    }


def _available_approvers(identity, requester, at):
    """此刻仍可独立批准的人，排除申请人本人。"""
    available = []
    for row in identity:
        actor = row.get("actor")
        if not _text(actor) or actor == requester:
            continue
        if row.get("role") not in APPROVER_ROLES or not _is_minutes(row.get("until")):
            continue
        if row["until"] >= at:
            available.append({"actor": actor, "role": row["role"], "until": row["until"]})
    return sorted(available, key=lambda item: item["actor"])


# --------------------------------------------------------------------------
# 批准与授权窗口
# --------------------------------------------------------------------------

def _approval_view(ticket, identity):
    actor = ticket["actor"]
    approved_by = ticket.get("approved_by")
    approved_at = ticket.get("approved_at")
    scheduled_end = ticket.get("scheduled_end")
    allowed = ticket.get("allowed")

    reasons = []
    approver = None
    verification = None

    if not _text(approved_by):
        reasons.append("no_independent_approval")
    if not _is_minutes(approved_at):
        reasons.append("approval_time_missing")
    if not isinstance(allowed, list) or not allowed or not _strings(allowed):
        reasons.append("scope_missing")
    if not _is_minutes(scheduled_end):
        reasons.append("scheduled_end_missing")

    if _text(approved_by):
        if approved_by == actor:
            reasons.append("self_approval")
        approver = _approver_view(identity, approved_by, approved_at if _is_minutes(approved_at) else 0)
        if not approver["known"]:
            reasons.append("approver_unknown")
        elif not approver["available_at"]:
            reasons.append("approver_not_available_at_approval")

    if _is_minutes(approved_at):
        verification = _latest_verification(identity, actor, not_after=approved_at)
        if verification is None:
            reasons.append("requester_not_verified_at_approval")
        else:
            until = verification.get("on_shift_until")
            if not _is_minutes(until):
                reasons.append("requester_shift_unknown_at_approval")
            elif until < approved_at:
                reasons.append("requester_not_on_shift_at_approval")
    if _is_minutes(approved_at) and _is_minutes(scheduled_end) and scheduled_end <= approved_at:
        reasons.append("window_empty")

    reasons = _dedupe(reasons)
    if not _text(approved_by) or not _is_minutes(approved_at):
        status = "unapproved"
    elif reasons:
        status = "invalid"
    else:
        status = "approved"

    return {
        "status": status,
        "reasons": reasons,
        "approved_by": approved_by if _text(approved_by) else None,
        "approved_at": approved_at if _is_minutes(approved_at) else None,
        "scheduled_end": scheduled_end if _is_minutes(scheduled_end) else None,
        "approver": approver,
        "requester_verification_at_approval": None if verification is None else {
            "id": verification["id"],
            "verified_at": verification["at"],
            "assurance": verification.get("assurance"),
            "on_shift_until": verification.get("on_shift_until") if _is_minutes(verification.get("on_shift_until")) else None,
        },
        "identity_record_used_as": "identity_only",
    }


def _end_view(ticket, requests):
    """事态结束记录只作用于同一单或同一事项；取最早的结束时间。

    带 ``effective_from`` 的记录只在其生效窗口内适用：生效时点晚于本次批准
    的结束记录属于之后重新批准的窗口，不能用来缩短当前这一单。
    """
    ticket_id = ticket["id"]
    case = ticket["case"]
    approved_at = ticket.get("approved_at")
    ends = []
    for row in requests:
        if row.get("id") == ticket_id or not _is_minutes(row.get("ended_at")):
            continue
        if "ticket" in row and row.get("ticket") is not None:
            applies = row.get("ticket") == ticket_id
        else:
            applies = row.get("case") == case
        if not applies:
            continue
        if _is_minutes(approved_at):
            if row["ended_at"] <= approved_at:
                continue
            effective_from = row.get("effective_from")
            if _is_minutes(effective_from) and effective_from > approved_at:
                continue
        ends.append(row)

    scheduled_end = ticket.get("scheduled_end")
    scheduled_end = scheduled_end if _is_minutes(scheduled_end) else None
    ends.sort(key=lambda row: (row["ended_at"], row["id"]))
    case_end = ends[0]["ended_at"] if ends else None
    issues = []
    effective_end = case_end
    if scheduled_end is not None:
        effective_end = scheduled_end if effective_end is None else min(effective_end, scheduled_end)
        if case_end is not None and case_end > scheduled_end:
            issues.append("end_after_scheduled_not_extended")
    if case_end is not None and _is_minutes(approved_at) and case_end <= approved_at:
        issues.append("end_not_after_approval")

    return {
        "scheduled_end": scheduled_end,
        "case_end": case_end,
        "effective_end": effective_end if _is_minutes(effective_end) else None,
        "ended_early": (case_end is not None and scheduled_end is not None and case_end < scheduled_end),
        "end_records": [row["id"] for row in ends],
        "issues": issues,
    }


def _cap_by_shift(end_view, approval, actor):
    """临时资格随当班窗口结束；不把当班登录延伸成长期岗位权限。"""
    verification = approval.get("requester_verification_at_approval")
    shift_until = verification.get("on_shift_until") if verification else None
    capped = dict(end_view)
    if shift_until is not None:
        effective = capped["effective_end"]
        if effective is None or shift_until < effective:
            capped["effective_end"] = shift_until
    return capped


def _status_at(approval, end_view, at):
    if approval["status"] != "approved":
        return approval["status"]
    approved_at = approval["approved_at"]
    effective_end = end_view["effective_end"]
    if effective_end is None:
        return "window_incomplete"
    if effective_end <= approved_at:
        return "window_empty"
    if at < approved_at:
        return "not_yet_effective"
    if at >= effective_end:
        return "expired"
    return "active"


def _points(ticket, checks):
    declared = ticket.get("points")
    if _strings(declared):
        return sorted(set(declared))
    observed = {row.get("point") for row in checks
                if row.get("ticket") == ticket["id"] and _text(row.get("point"))}
    return sorted(observed)


# --------------------------------------------------------------------------
# 各端收回
# --------------------------------------------------------------------------

def _receipts(ticket, points, checks, effective_end, at):
    """每个端点的收回回执状态；未确认、确认时点早于结束、晚于查询都需跟进。"""
    entries = []
    for point in points:
        claims = [row for row in checks
                  if row.get("ticket") == ticket["id"] and row.get("point") == point
                  and "revocation_applied_at" in row]
        evaluated = []
        acknowledged_times = []
        for claim in claims:
            applied = claim.get("revocation_applied_at")
            if applied is None:
                verdict = "pending"
            elif not _is_minutes(applied):
                verdict = "invalid_value"
            elif effective_end is None:
                verdict = "not_required"
            elif applied < effective_end:
                verdict = "applied_before_end"
            elif applied > at:
                verdict = "not_observed_yet"
            else:
                verdict = "acknowledged"
                acknowledged_times.append(applied)
            evaluated.append({
                "id": claim["id"],
                "revocation_applied_at": applied,
                "verdict": verdict,
            })
        if acknowledged_times:
            state = "acknowledged"
        elif any(item["verdict"] in ("invalid_value", "applied_before_end", "not_observed_yet")
                 for item in evaluated):
            state = "invalid"
        elif effective_end is None:
            state = "not_required"
        else:
            state = "pending"
        entries.append({
            "point": point,
            "state": state,
            "applied_at": min(acknowledged_times) if acknowledged_times else None,
            "claims": evaluated,
            "needs_follow_up": state != "acknowledged",
        })
    return entries


def _revocation_view(ticket, points, checks, end_view, at):
    effective_end = end_view["effective_end"]
    entries = _receipts(ticket, points, checks, effective_end, at)
    return {
        "required": effective_end is not None,
        "effective_end": effective_end,
        "ended_early": end_view["ended_early"],
        "points": entries,
        "acknowledged": [entry["point"] for entry in entries if entry["state"] == "acknowledged"],
        "pending_for_follow_up": [entry["point"] for entry in entries if entry["needs_follow_up"]],
    }


# --------------------------------------------------------------------------
# 操作记录：保留原报告，逐条说明操作当时是否有依据
# --------------------------------------------------------------------------

def _operation_events(ticket, approval, end_view, points, checks, at):
    effective_end = end_view["effective_end"]
    events = []
    for row in checks:
        if row.get("ticket") != ticket["id"]:
            continue
        if not (_text(row.get("operation")) and _is_minutes(row.get("at"))):
            continue
        reasons = []
        if approval["status"] != "approved":
            reasons.append("authorization_invalid")
        if approval["approved_at"] is not None and row["at"] < approval["approved_at"]:
            reasons.append("before_approval")
        if effective_end is not None and row["at"] >= effective_end:
            reasons.append("after_effective_end")
        if row.get("case") != ticket["case"]:
            reasons.append("case_mismatch")
        if row.get("actor") is not None and row.get("actor") != ticket["actor"]:
            reasons.append("not_ticket_holder")
        if row.get("region") is not None and row.get("region") != ticket["region"]:
            reasons.append("region_outside_authorization")
        allowed = ticket.get("allowed")
        if _strings(allowed) and row["operation"] not in allowed:
            reasons.append("operation_outside_scope")
        if row.get("point") is not None and _strings(ticket.get("points")) and row["point"] not in ticket["points"]:
            reasons.append("point_outside_scope")

        endpoint_revoked = False
        if _text(row.get("point")) and effective_end is not None:
            receipts = _receipts(ticket, [row["point"]], checks, effective_end, at)
            endpoint_revoked = any(
                entry["applied_at"] is not None and entry["applied_at"] <= row["at"]
                for entry in receipts
            )
            if endpoint_revoked:
                reasons.append("endpoint_revoked_before_operation")

        reported_at = row.get("reported_at") if _is_minutes(row.get("reported_at")) else None
        events.append({
            "id": row["id"],
            "report": copy.deepcopy(row),
            "point": row.get("point"),
            "operation": row["operation"],
            "at": row["at"],
            "case": row.get("case"),
            "region": row.get("region"),
            "reported_at": reported_at,
            "occurred_before_revocation": (
                None if effective_end is None else row["at"] < effective_end
            ),
            "occurred_after_revocation": (
                None if effective_end is None else row["at"] >= effective_end
            ),
            "reported_after_revocation": (
                None if reported_at is None or effective_end is None else reported_at >= effective_end
            ),
            "had_basis_at_operation": not reasons,
            "reasons": _dedupe(reasons),
        })
    return sorted(events, key=lambda event: (event["at"], event["id"]))


# --------------------------------------------------------------------------
# 核验入口
# --------------------------------------------------------------------------

def assess(sources, ticket_id, at, *, actor=None, case=None, region=None,
           operation=None, point=None, include_events=True):
    """核验某单在 ``at`` 时点的资格，并把一次拟议查阅与申请人／事项／限制对齐。

    ``region``、``actor``、``case``、``operation``、``point`` 是本次实际查阅
    请求的内容；任何一个与批准不一致都会拒绝，且不会把资格转给他人或别处。
    """
    at = _minutes(at, "查询时点")
    identity = _source(sources, "identity")
    requests = _source(sources, "requests")
    checks = _source(sources, "checks")
    ticket = _find_ticket(requests, ticket_id)

    approval = _approval_view(ticket, identity)
    end_view = _cap_by_shift(_end_view(ticket, requests), approval, ticket["actor"])
    status = _status_at(approval, end_view, at)
    requester = _requester_view(identity, ticket["actor"], at)
    approvers_now = _available_approvers(identity, ticket["actor"], at)
    points = _points(ticket, checks)
    revocation = _revocation_view(ticket, points, checks, end_view, at)

    request = {"actor": actor, "case": case, "region": region,
               "operation": operation, "point": point}
    denials = []
    if approval["status"] != "approved":
        denials.extend(approval["reasons"])
    elif status != "active":
        denials.append(status)
    if actor is not None and actor != ticket["actor"]:
        denials.append("not_ticket_holder")
    if case is not None and case != ticket["case"]:
        denials.append("case_mismatch")
    if region is not None and region != ticket["region"]:
        denials.append("region_outside_authorization")
    if operation is not None and operation not in (ticket.get("allowed") or []):
        denials.append("operation_outside_scope")
    if point is not None and _strings(ticket.get("points")) and point not in ticket["points"]:
        denials.append("point_outside_scope")
    denials = _dedupe(denials)

    identity_used = requester["last_completed_verification"] is not None
    if identity_used and approval["status"] != "approved":
        denials.append("identity_record_is_not_authorization")
        denials = _dedupe(denials)

    authorized = not denials
    region_visible = region is not None and region != ticket["region"]
    scope = None
    if not region_visible:
        scope = {
            "case": ticket["case"],
            "region": ticket["region"],
            "operations": list(ticket.get("allowed") or []),
            "points": list(points),
            "until": end_view["effective_end"],
        }

    note = None
    if not authorized and status == "expired":
        note = "到期资格不得用于新的查阅，不因接收端尚未确认而延长"
    elif not authorized and region_visible:
        note = "多机构只返回本机构记录，紧急访问不沿用到其他区域"

    confirmation = {
        "at": at,
        "request": request,
        "authorized": authorized,
        "refusal_reasons": denials,
        "requester": requester,
        "approval": approval,
        "approvers_available_now": approvers_now,
        "bound_to": {
            "actor": ticket["actor"],
            "case": ticket["case"],
            "region": ticket["region"],
            "operations": list(ticket.get("allowed") or []),
            "points": list(points),
        },
        "not_inheritable": True,
        "identity_record_used_as": "identity_only",
        "long_term_effect": "none",
    }

    current_access = {
        "authorized": authorized,
        "status": status,
        "until": end_view["effective_end"],
        "case": ticket["case"] if not region_visible else None,
        "region": ticket["region"] if not region_visible else None,
        "operations": list(ticket.get("allowed") or []) if not region_visible else [],
        "point": point,
        "disclosure_scope": "none" if region_visible else ticket["region"],
        "note": note,
    }

    events = _operation_events(ticket, approval, end_view, points, checks, at) if include_events else []

    return {
        "query": at,
        "ticket": {
            "id": ticket["id"],
            "case": ticket["case"],
            "region": ticket["region"],
            "status": status,
            "approval": approval,
            "scheduled_end": end_view["scheduled_end"],
            "case_end": end_view["case_end"],
            "effective_end": end_view["effective_end"],
            "ended_early": end_view["ended_early"],
            "end_records": end_view["end_records"],
            "issues": end_view["issues"],
            "allowed": list(ticket.get("allowed") or []),
            "points": points,
        },
        "requester": requester,
        "approval": approval["approver"],
        "confirmation": confirmation,
        "current_access": current_access,
        "revocation": revocation,
        "events": events,
    }


verify = assess


def list_region(sources, region, at=None):
    """只列出该机构自己的业务单，不返回其他区域的记录。"""
    if not _text(region):
        raise VerificationError("机构区域不能为空")
    if at is not None:
        at = _minutes(at, "查询时点")
    identity = _source(sources, "identity")
    requests = _source(sources, "requests")
    checks = _source(sources, "checks")
    visible = []
    for ticket in requests:
        if ticket.get("region") != region:
            continue
        if not (_text(ticket.get("id")) and _text(ticket.get("actor")) and _text(ticket.get("case"))):
            continue
        approval = _approval_view(ticket, identity)
        end_view = _cap_by_shift(_end_view(ticket, requests), approval, ticket["actor"])
        status = approval["status"] if at is None else _status_at(approval, end_view, at)
        visible.append({
            "id": ticket["id"],
            "case": ticket["case"],
            "region": ticket["region"],
            "status": status,
            "scheduled_end": end_view["scheduled_end"],
            "effective_end": end_view["effective_end"],
            "allowed": list(ticket.get("allowed") or []),
            "points": _points(ticket, checks),
        })
    return {"region": region, "tickets": visible}


records_for_region = list_region


# --------------------------------------------------------------------------
# 处置入口：登记提前结束与各端回执
# --------------------------------------------------------------------------

def _latest_observed(checks, fallback):
    times = [fallback]
    for row in checks:
        for field in ("at", "reported_at", "revocation_applied_at"):
            if _is_minutes(row.get(field)):
                times.append(row[field])
    return max(times)


def _unique_id(base, rows):
    used = {row.get("id") for row in rows}
    if base not in used:
        return base
    index = 2
    while f"{base}-{index}" in used:
        index += 1
    return f"{base}-{index}"


def _parse_receipts(receipts):
    if receipts is None:
        return []
    parsed = []
    for item in receipts:
        if not isinstance(item, dict) or not _text(item.get("point")):
            raise VerificationError("回执需要 point 与 applied_at")
        applied = item.get("applied_at")
        if applied is not None and not _is_minutes(applied):
            raise VerificationError("applied_at 需要非负整数分钟或 null")
        parsed.append({"point": item["point"], "applied_at": applied})
    return parsed


def handle(sources, ticket_id, ended_at, *, at=None, receipts=None):
    """登记提前结束与经办收到的各端回执，返回仍需跟进的端点。

    只产出拟登记记录和判断，不覆盖原始资料；需要留存时用 :func:`apply_plan`。
    若 ``at`` 省略，使用资料中已观察到的最晚时点（但不早于 ``ended_at``），
    以便区分“已确认”“尚未确认”和“晚于查询才确认”。
    """
    ended_at = _minutes(ended_at, "提前结束时间")
    identity = _source(sources, "identity")
    requests = _source(sources, "requests")
    checks = _source(sources, "checks")
    ticket = _find_ticket(requests, ticket_id)
    approval = _approval_view(ticket, identity)
    if approval["status"] != "approved":
        raise VerificationError("该记录没有可收回的独立批准")
    approved_at = approval["approved_at"]
    if ended_at < approved_at:
        raise VerificationError("提前结束时间不能早于批准时间")

    observed = None
    if at is None:
        at = _latest_observed(checks, ended_at)
        observed = "material"
    at = _minutes(at, "查询时点")
    if at < ended_at:
        raise VerificationError("查询时点不能早于登记结束时间")

    base_end = _cap_by_shift(_end_view(ticket, requests), approval, ticket["actor"])
    scheduled_end = base_end["scheduled_end"]
    notes = []
    effective_end = ended_at
    if scheduled_end is not None and ended_at > scheduled_end:
        effective_end = scheduled_end
        notes.append("end_after_scheduled_not_extended")
    if base_end["case_end"] is not None and base_end["case_end"] < effective_end:
        effective_end = base_end["case_end"]
        notes.append("existing_earlier_end_used")
    if effective_end <= approved_at:
        raise VerificationError("结束时间落在批准之前，授权窗口为空")

    parsed_receipts = _parse_receipts(receipts)
    simulated_checks = [copy.deepcopy(row) for row in checks]
    receipt_records = []
    for item in parsed_receipts:
        duplicate = any(
            row.get("ticket") == ticket_id
            and row.get("point") == item["point"]
            and row.get("revocation_applied_at") == item["applied_at"]
            for row in checks
        )
        if duplicate:
            continue
        record = {
            "id": _unique_id(
                f"receipt-{ticket_id}-{item['point']}-{item['applied_at']}", checks + receipt_records),
            "ticket": ticket_id,
            "point": item["point"],
            "revocation_applied_at": item["applied_at"],
        }
        receipt_records.append(record)
        simulated_checks.append(record)

    points = sorted(set(_points(ticket, checks)) | {item["point"] for item in parsed_receipts})
    revocation = _receipts(ticket, points, simulated_checks, effective_end, at)
    pending = [entry["point"] for entry in revocation if entry["needs_follow_up"]]
    acknowledged = [entry["point"] for entry in revocation if entry["state"] == "acknowledged"]

    existing_end = next(
        (row for row in requests if row.get("id") in base_end["end_records"]
         and row.get("ended_at") == ended_at),
        None,
    )
    if existing_end is None:
        end_record = {
            "id": _unique_id(f"end-{ticket_id}-{ended_at}", requests),
            "ticket": ticket_id,
            "case": ticket["case"],
            "ended_at": ended_at,
            "effective_from": approved_at,
        }
    else:
        end_record = None

    new_access = {
        "authorized": approval["status"] == "approved" and at < effective_end,
        "at": at,
        "until": effective_end,
        "note": ("到期资格不得用于新的查阅，不因接收端尚未确认而延长"
                 if at >= effective_end else None),
    }

    return {
        "ticket": ticket_id,
        "query": at,
        "query_source": observed or "explicit",
        "registration": {
            "ended_at": ended_at,
            "effective_end": effective_end,
            "end_record": end_record,
            "already_recorded_end": existing_end["id"] if existing_end is not None else None,
            "receipt_records": receipt_records,
            "notes": notes,
            "persisted": False,
        },
        "receipts": revocation,
        "acknowledged": acknowledged,
        "pending_for_follow_up": pending,
        "new_access": new_access,
    }


revoke = handle


def apply_plan(sources, plan, directory):
    """把处置结果另存为新资料目录；原始来源不被覆盖。"""
    if not isinstance(plan, dict) or "registration" not in plan:
        raise VerificationError("处置结果格式无效")
    updated = {name: copy.deepcopy(rows) for name, rows in sources.items()}
    registration = plan["registration"]
    if registration.get("end_record") is not None:
        updated.setdefault("requests", []).append(copy.deepcopy(registration["end_record"]))
    for record in registration.get("receipt_records") or []:
        updated.setdefault("checks", []).append(copy.deepcopy(record))
    for name, rows in updated.items():
        identifiers = [row.get("id") for row in rows]
        if len(identifiers) != len(set(identifiers)):
            raise VerificationError(f"{name} 的记录标识重复")
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    for name, rows in updated.items():
        (target / f"{name}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"directory": str(target), "sources": sorted(updated)}


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------

def _parse_receipt_option(value):
    if "@" not in value:
        raise argparse.ArgumentTypeError("回执格式为 POINT@TIME 或 POINT@pending")
    point, applied = value.rsplit("@", 1)
    if not point:
        raise argparse.ArgumentTypeError("回执缺少端点")
    if applied == "pending":
        return {"point": point, "applied_at": None}
    try:
        applied_at = int(applied)
    except ValueError:
        raise argparse.ArgumentTypeError("回执时间需要整数分钟或 pending") from None
    if applied_at < 0:
        raise argparse.ArgumentTypeError("回执时间需要非负整数分钟")
    return {"point": point, "applied_at": applied_at}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=Path(__file__).with_name("records"))
    parser.add_argument("--ticket", help="业务单标识")
    parser.add_argument("--at", type=int, help="查询时点（相对分钟）")
    parser.add_argument("--actor", help="本次实际查阅人")
    parser.add_argument("--case", help="本次实际事项")
    parser.add_argument("--region", help="本次查阅的机构区域")
    parser.add_argument("--operation", help="本次实际操作")
    parser.add_argument("--point", help="本次实际端点")
    parser.add_argument("--end-at", type=int, help="登记提前结束时间")
    parser.add_argument("--receipt", action="append", type=_parse_receipt_option,
                        help="登记某端撤回结果，可重复，格式 POINT@TIME 或 POINT@pending")
    parser.add_argument("--list-region", help="只列出该机构区域的业务单")
    parser.add_argument("--out", type=Path, help="将处置结果另存到该目录")
    parser.add_argument("--no-events", action="store_true", help="不返回操作记录")
    args = parser.parse_args(argv)

    try:
        sources = load_sources(args.records)
        if args.ticket is not None and args.end_at is None and args.out is not None:
            raise VerificationError("只有登记结束与回执时才能 --out")
        if args.list_region is not None:
            result = list_region(sources, args.list_region, at=args.at)
        elif args.ticket is not None:
            if args.end_at is not None:
                result = handle(sources, args.ticket, args.end_at, at=args.at,
                                receipts=args.receipt)
                if args.out is not None:
                    result["applied"] = apply_plan(sources, result, args.out)
            else:
                if args.at is None:
                    raise VerificationError("核验需要 --at")
                result = assess(sources, args.ticket, args.at,
                                actor=args.actor, case=args.case, region=args.region,
                                operation=args.operation, point=args.point,
                                include_events=not args.no_events)
        else:
            raise VerificationError("需要 --ticket、--end-at 或 --list-region")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
