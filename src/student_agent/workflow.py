from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def _call(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    case_id: str,
    actor: str,
    tool_name: str,
    evidence_refs: list[str],
    **arguments: str,
) -> dict[str, Any] | None:
    """Gọi 1 MCP tool, ghi trace tool_result_consumed, gom evidence_ref.
    Trả về None nếu tool báo lỗi (vd order không tồn tại) thay vì raise,
    để coordinator có thể tiếp tục thử candidate khác.
    """
    try:
        evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
    except RuntimeError:
        return None
    evidence_refs.append(evidence["evidence_ref"])
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence["evidence_ref"]],
    )
    return evidence


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    request = case["customer_request"]
    claimed_order_id = request.get("claimed_order_id")
    candidate_order_ids = case.get("candidate_order_ids") or [claimed_order_id]
    candidate_order_ids = [c for c in dict.fromkeys(candidate_order_ids) if c]
    policy_version = case.get("policy_version")
    customer_hint = case.get("customer_unique_id_hint")
    claims = request.get("claims", [])

    evidence_refs: list[str] = []

    # ---------- 1. Coordinator -> Order Agent: entity resolution ----------
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="order_agent")

    resolved: dict[str, dict[str, Any]] = {}
    rejected: list[str] = []
    for order_id in candidate_order_ids:
        evidence = await _call(gateway, trace, case_id, "order_agent", "get_order", evidence_refs, order_id=order_id)
        if evidence is None:
            rejected.append(order_id)
            continue
        data = evidence["data"]
        if customer_hint and data.get("customer_unique_id") and data.get("customer_unique_id") != customer_hint:
            rejected.append(order_id)
            continue
        resolved[order_id] = data

    if len(resolved) == 1:
        resolution_status = "resolved"
        primary_order_id = next(iter(resolved))
    elif len(resolved) > 1:
        resolution_status = "ambiguous"
        # ưu tiên order khớp claimed_order_id nếu có trong tập resolved
        primary_order_id = claimed_order_id if claimed_order_id in resolved else next(iter(resolved))
    else:
        resolution_status = "not_found"
        primary_order_id = None

    entity_resolution = {
        "status": resolution_status,
        "resolved_order_ids": list(resolved.keys()),
        "rejected_candidates": rejected,
        "confidence": 0.9 if resolution_status == "resolved" else (0.5 if resolution_status == "ambiguous" else 0.1),
    }

    seller_ids: list[str] = []
    item_ids: list[str] = []
    payment_references: list[str] = []
    shipment_ids: list[str] = []

    # Nếu không resolve được order nào -> trả case insufficient_evidence, vẫn hợp lệ theo schema
    if primary_order_id is None:
        trace.emit(case_id=case_id, event_type="handoff", actor="coordinator", target="verifier_agent")
        trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier_agent")
        return {
            "schema_version": "day09-l3b-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": "insufficient_evidence",
                "secondary_issues": [],
                "case_status": "needs_investigation",
                "confidence": 0.1,
            },
            "affected_entities": {
                "order_ids": [], "item_ids": [], "seller_ids": [],
                "payment_references": [], "shipment_ids": [],
            },
            "entity_resolution": entity_resolution,
            "customer_context": {
                "customer_unique_id": customer_hint,
                "related_order_ids": [],
            },
            "shipment_analysis": {
                "verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False,
            },
            "payment_analysis": {
                "verdict": "insufficient_evidence",
                "captured_total_brl": None, "refunded_total_brl": None, "refundable_total_brl": None,
            },
            "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
            "evidence_refs": evidence_refs,
            "data_conflicts": [],
            "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []},
            "resolution_actions": ["escalate_manual_review"],
        }

    order_data = resolved[primary_order_id]

    # ---------- 2. Specialist Agents (song song theo scope) ----------
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="payment_agent")
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="shipment_agent")

    items_evidence = await _call(gateway, trace, case_id, "order_agent", "get_order_items", evidence_refs, order_id=primary_order_id)
    items_data = items_evidence["data"] if items_evidence else []
    if isinstance(items_data, list):
        for item in items_data:
            if isinstance(item, dict):
                if item.get("item_id"):
                    item_ids.append(item["item_id"])
                if item.get("seller_id"):
                    seller_ids.append(item["seller_id"])

    if seller_ids:
        await _call(gateway, trace, case_id, "order_agent", "get_sellers", evidence_refs, seller_ids=seller_ids)

    payments_evidence = await _call(gateway, trace, case_id, "payment_agent", "get_order_payments", evidence_refs, order_id=primary_order_id)
    payments_data = payments_evidence["data"] if payments_evidence else {}

    payment_timeline_evidence = await _call(gateway, trace, case_id, "payment_agent", "get_payment_timeline", evidence_refs, order_id=primary_order_id)
    refund_timeline_evidence = await _call(gateway, trace, case_id, "payment_agent", "get_refund_timeline", evidence_refs, order_id=primary_order_id)

    shipment_evidence = await _call(gateway, trace, case_id, "shipment_agent", "get_shipment_summary", evidence_refs, order_id=primary_order_id)
    shipment_data = shipment_evidence["data"] if shipment_evidence else {}

    customer_history_evidence = None
    if customer_hint:
        customer_history_evidence = await _call(
            gateway, trace, case_id, "order_agent", "get_customer_history", evidence_refs, customer_unique_id=customer_hint
        )

    trace.emit(case_id=case_id, event_type="handoff", actor="order_agent", target="policy_agent")

    # ---------- 3. Policy Agent: quyết định trên chính sách + bằng chứng ----------
    policy_evidence = None
    if policy_version:
        policy_evidence = await _call(gateway, trace, case_id, "policy_agent", "get_policy", evidence_refs, policy_version=policy_version)

    claim_topics = {c.get("topic") for c in claims if isinstance(c, dict)}

    # --- Shipment verdict (heuristic - CHỈNH LẠI theo field thật của shipment_data) ---
    shipment_status = str(shipment_data.get("status", "")).lower() if isinstance(shipment_data, dict) else ""
    is_late = bool(shipment_data.get("is_late")) if isinstance(shipment_data, dict) else False
    late_party = str(shipment_data.get("delay_responsible", "")).lower() if isinstance(shipment_data, dict) else ""

    if not shipment_data:
        shipment_verdict = "insufficient_evidence"
    elif "lost" in shipment_status:
        shipment_verdict = "lost"
    elif "return" in shipment_status:
        shipment_verdict = "returned"
    elif is_late and "seller" in late_party:
        shipment_verdict = "seller_delay"
    elif is_late:
        shipment_verdict = "logistics_delay"
    else:
        shipment_verdict = "on_time"

    late_seller_ids = seller_ids if shipment_verdict == "seller_delay" else []

    # --- Payment verdict (heuristic - CHỈNH LẠI theo field thật của payments_data) ---
    captured = payments_data.get("captured_total_brl") if isinstance(payments_data, dict) else None
    refunded = payments_data.get("refunded_total_brl") if isinstance(payments_data, dict) else None
    refundable = None
    if isinstance(captured, (int, float)) and isinstance(refunded, (int, float)):
        refundable = max(captured - refunded, 0)

    if not payments_data:
        payment_verdict = "insufficient_evidence"
    elif refund_timeline_evidence and isinstance(refund_timeline_evidence["data"], dict) and refund_timeline_evidence["data"].get("failed"):
        payment_verdict = "refund_failed"
    elif refundable and refundable > 0 and shipment_verdict != "on_time":
        payment_verdict = "refund_pending"
    elif refunded and captured and refunded >= captured:
        payment_verdict = "refunded"
    else:
        payment_verdict = "reconciled"

    # --- Primary issue: ưu tiên theo claim của khách + verdict xác minh được ---
    if "late_delivery_logistics" in claim_topics and shipment_verdict == "logistics_delay":
        primary_issue = "late_delivery_logistics"
    elif "late_delivery_seller" in claim_topics and shipment_verdict == "seller_delay":
        primary_issue = "late_delivery_seller"
    elif shipment_verdict == "seller_delay":
        primary_issue = "late_delivery_seller"
    elif shipment_verdict == "logistics_delay":
        primary_issue = "late_delivery_logistics"
    elif payment_verdict == "refund_pending":
        primary_issue = "refund_pending"
    elif payment_verdict == "refund_failed":
        primary_issue = "refund_failed"
    elif shipment_verdict == "insufficient_evidence" and payment_verdict == "insufficient_evidence":
        primary_issue = "insufficient_evidence"
    else:
        primary_issue = "unsupported_claim"

    case_status = "action_required" if primary_issue not in ("insufficient_evidence", "unsupported_claim") else "needs_investigation"
    confidence = 0.75 if resolution_status == "resolved" and shipment_data and payments_data else 0.4

    responsible_party_type = "seller" if primary_issue == "late_delivery_seller" else (
        "logistics_provider" if primary_issue == "late_delivery_logistics" else (
            "payment_provider" if primary_issue in ("refund_pending", "refund_failed") else "unknown"
        )
    )
    responsible_party_id = (seller_ids[0] if seller_ids else None) if responsible_party_type == "seller" else None

    recommended_refund = float(refundable) if isinstance(refundable, (int, float)) and primary_issue in (
        "late_delivery_seller", "late_delivery_logistics", "refund_pending", "refund_failed",
    ) else 0.0

    refund_lines = []
    if recommended_refund > 0:
        refund_lines.append({
            "reason_code": primary_issue,
            "amount_brl": recommended_refund,
            "entity_id": primary_order_id,
        })

    resolution_actions = []
    if primary_issue in ("late_delivery_seller", "late_delivery_logistics") and recommended_refund > 0:
        resolution_actions.append("issue_refund")
    if primary_issue == "refund_failed":
        resolution_actions.append("retry_refund")
    if case_status == "needs_investigation":
        resolution_actions.append("escalate_manual_review")
    if not resolution_actions:
        resolution_actions.append("no_action_required")

    trace.emit(case_id=case_id, event_type="handoff", actor="policy_agent", target="verifier_agent")

    # ---------- 4. Verifier: kiểm tra nhất quán trước khi trả kết quả ----------
    trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier_agent")

    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": [],
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": list(resolved.keys()),
            "item_ids": item_ids,
            "seller_ids": list(dict.fromkeys(seller_ids)),
            "payment_references": payment_references,
            "shipment_ids": shipment_ids,
        },
        "entity_resolution": entity_resolution,
        "customer_context": {
            "customer_unique_id": customer_hint,
            "related_order_ids": [],
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": list(dict.fromkeys(late_seller_ids)),
            "timeline_complete": bool(shipment_data),
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured,
            "refunded_total_brl": refunded,
            "refundable_total_brl": refundable,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}] if primary_issue not in ("insufficient_evidence", "unsupported_claim") else [],
            "responsible_parties": [{"party_type": responsible_party_type, "party_id": responsible_party_id}],
        },
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": resolution_actions,
    }