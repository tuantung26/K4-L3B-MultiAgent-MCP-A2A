# L3B Architecture Record — K4 Lớp 13B

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

---

## 1. System overview

```text
Input JSON
    │
    ▼
┌──────────────────────────────────────┐
│         Coordinator / Router         │  ← LangGraph StateGraph
│         (solve_case in workflow.py)  │
└─────────────────────┬────────────────┘
                      │ entity_resolver node
                      ▼
         ┌────────────────────────┐
         │    Entity Resolver     │  tools: get_order_details,
         │    (entity_resolver.py)│         get_customer_history,
         └────────────┬───────────┘         search_orders_by_customer
                      │ handoff → specialists_parallel
                      ▼ (asyncio.gather — 3 concurrent nodes)
    ┌─────────────────┬────────────────────┬──────────────────────┐
    ▼                 ▼                    ▼
┌──────────┐  ┌───────────────┐  ┌─────────────────┐
│Order/Item│  │  Shipment     │  │    Payment      │
│  Agent   │  │  Agent        │  │    Agent        │
└────┬─────┘  └──────┬────────┘  └────────┬────────┘
     │               │                    │
     │  tools:        │  tools:             │  tools:
     │  get_order_    │  get_shipment_      │  get_payment_details
     │  details,      │  tracking,          │  get_refund_status
     │  get_order_    │  get_seller_info    │
     │  items,        │                    │
     │  get_seller_   │                    │
     │  info          │                    │
     └───────────────┬┘────────────────────┘
                     │ merge + handoff → policy
                     ▼
           ┌──────────────────┐
           │   Policy Agent   │  tools: get_policy_rules
           │  (policy_agent.py│
           └────────┬─────────┘
                    │ handoff → verifier
                    ▼
           ┌──────────────────┐
           │  Verifier Agent  │  No MCP calls — pure validation
           │ (verifier_agent) │
           └────────┬─────────┘
                    │ case_finalized trace event
                    ▼
              [END OUTPUT]
              outputs/<case_id>.json
              traces/trace.jsonl
```

MCP Gateway được gọi từ từng specialist node; mọi call đều gắn `case_id`, được audit và ghi `tool_result_consumed` vào trace.

---

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| **Coordinator** | `case` JSON raw | Khởi tạo LangGraph, orchestrate pipeline, assemble final output | Không gọi MCP trực tiếp | invoke entity_resolver → specialists → policy → verifier |
| **Entity Resolver** | `order_id_candidates`, `customer_id` hints | Resolve candidates thành `resolved_order_ids`; reject ambiguous; lấy customer history | `get_order_details`, `get_customer_history` | `resolved_order_ids`, `rejected_candidates`, `entity_resolution_status`, `entity_confidence` |
| **Order/Item Agent** | `resolved_order_ids` | Lấy chi tiết order + item; phân loại `primary_issue` | `get_order_details`, `get_order_items`, `get_seller_info` | `order_data`, `item_data`, `primary_issue`, `secondary_issues` |
| **Shipment Agent** | `resolved_order_ids` | Lấy tracking; phân loại `shipment_verdict`; xác định `late_seller_ids` | `get_shipment_tracking`, `get_seller_info` | `shipment_verdict`, `late_seller_ids`, `shipment_timeline_complete` |
| **Payment Agent** | `resolved_order_ids` | Lấy payment + refund; tính totals; phân loại `payment_verdict` | `get_payment_details`, `get_refund_status` | `payment_verdict`, `captured_total_brl`, `refunded_total_brl`, `refundable_total_brl` |
| **Policy Agent** | toàn bộ specialist output | Gọi policy rules; tổng hợp root cause, responsible parties, data conflicts, financial resolution, resolution actions | `get_policy_rules` | `root_cause_ranked`, `responsible_parties`, `financial_resolution`, `resolution_actions`, `case_status`, `overall_confidence` |
| **Verifier** | toàn bộ assembled state | Kiểm tra invariants trước finalize; không gọi MCP | Không gọi MCP | `verification_passed`, `verification_errors` |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

---

## 3. Entity resolution và A2A protocol

**Ranking candidates:**
1. Trích xuất từ `case.order_id` (exact) + `case.order_id_candidates` (danh sách ưu tiên).
2. Với mỗi candidate gọi `get_order_details`; nếu trả về `data` không rỗng → resolved; ngược lại → rejected.
3. Cap tối đa 10 candidates để tránh quét rộng.

**Confidence threshold:**
- 1 resolved → status=`resolved`, confidence=0.90
- >1 resolved → status=`ambiguous`, confidence=0.55
- 0 resolved → status=`not_found`, confidence=0.10

**Message envelope (A2A handoff):**
- LangGraph truyền toàn bộ `CaseState` (TypedDict) giữa các node.
- Correlation theo `case_id` — mọi MCP call và trace event đều mang `case_id`.
- Handoff được ghi bằng `trace.emit(event_type="handoff", actor=..., target=..., decision_code=...)`.

**Timeout & loop avoidance:**
- Retry budget per tool: 2 lần (3 attempts total); exception được append vào `state.errors`.
- LangGraph graph là DAG (không có cycle) → không thể xảy ra vòng lặp vô tận.
- Evidence không được tái sử dụng giữa các case (mỗi `solve_case()` khởi tạo state mới).

---

## 4. Evidence và conflict lifecycle

1. **Validate MCP response**: `EvidenceGateway.call()` gọi `contracts.validate_evidence()` ngay khi nhận kết quả; exception nếu không hợp lệ.
2. **Lưu evidence_ref**: Mỗi agent append `evidence["evidence_ref"]` vào `<domain>_evidence_refs` và `all_evidence_refs`.
3. **Emit `tool_result_consumed`**: Ngay sau mỗi thành công gateway call, agent emit trace event với `evidence_refs=[ev["evidence_ref"]]`.
4. **Source conflict**: Phát hiện trong Policy Agent (vd. shipment_verdict="conflicting"); ghi vào `data_conflicts` với `selected_source` và `resolution_code`.
5. **Map evidence → claim**: `evidence_refs` trong output chứa tất cả refs dùng để support kết luận.
6. **No cross-case reuse**: State được khởi tạo từ đầu mỗi `solve_case()`. `all_evidence_refs` chỉ valid trong scope một case.

---

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / network error | 2 retries (3 attempts) | Skip tool; append error to `state.errors`; agent returns `insufficient_evidence` for that domain | `errors` list in state |
| Entity not found/ambiguous | 0 retries | status=`not_found`/`ambiguous`; confidence=0.10/0.55; case_status=`needs_investigation` | `handoff` decision_code=not_found |
| Source conflict | 0 retries | Policy agent chọn `selected_source` theo priority rule; ghi `data_conflicts` | `policy_decided` |
| Invalid specialist result | 0 retries | Verifier bắt lỗi; `verification_passed=False`; errors logged | `verification_completed` FAIL |

**Query budget / cache strategy:**
- Mỗi tool được gọi tối đa 1 lần per `order_id` per agent (không gọi trùng).
- Order details được gọi bởi entity_resolver; order_item_node reuse dữ liệu từ `state.order_data` (không call lại).
- Cap số order xử lý: 5 orders per agent.
- Specialists chạy song song (`asyncio.gather`) → giảm latency tổng thể.

---

## 6. Verification invariants

Verifier kiểm tra trước finalize:

1. `entity_resolution_status != "not_found"` khi `resolved_order_ids` không rỗng.
2. Tất cả `evidence_ref` khớp pattern `^ev_[A-Za-z0-9_-]{20,96}$`.
3. `evidence_refs` không có duplicate trong cùng case.
4. Mỗi `cause_code` trong `root_cause_ranked` khớp pattern `^[A-Z][A-Z0-9_]{2,79}$`.
5. Ranks trong `root_cause_ranked` là unique.
6. `overall_confidence` trong [0.0, 1.0].
7. `financial_resolution.recommended_refund_brl >= 0`.
8. `resolution_actions` không rỗng; mỗi action có độ dài 1–80 ký tự.
9. `case_status` thuộc enum `{action_required, no_action, needs_investigation}`.

---

## 7. Reproducibility

**Framework:** LangGraph `>=0.2,<1` + langchain-core `>=0.3,<1` + Python 3.11+

**Model:** Không sử dụng LLM call trong pipeline — toàn bộ logic là rule-based / deterministic từ MCP evidence. Điều này đảm bảo reproducibility tuyệt đối (cùng MCP response → cùng output).

**Dependency pinning:** Xem `pyproject.toml`. Để pin chính xác:
```bash
pip freeze > requirements.lock
```

**Concurrency limit:** 3 specialists chạy song song (`asyncio.gather`); mỗi specialist cap 5 orders và 2 retries per tool.

**Lệnh chạy:**
```bash
# Activate venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux/macOS

# Validate inputs
day09 validate-inputs

# Run all cases
day09 run

# Validate outputs
day09 validate

# Package for submission
day09 package --output dist/submission.zip
```

**Không ghi API key vào repo. Mọi secret ở `.env` (gitignored).**
