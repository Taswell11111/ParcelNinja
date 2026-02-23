import streamlit as st
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta
import concurrent.futures
import json

# ─────────────────────────────────────────────────────────────────────────────
# STORE CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

STORES = {
    "Diesel": {
        "username": "ENgsxyMbeqVGvGzTCpVdkZmsjz/VCDeF+NWHlRk3Hk0=",
        "password": "EuoTNvCvp5imhOR2TZDe/fnKDxfoPK+EORSqfGvafZk=",
        "store_id": "7b0fb2ac-51bd-47ea-847e-cfb1584b4aa2",
    },
    "Hurley": {
        "username": "CtAAy94MhKTJClgAwEfQL9LfkM14CegkeUbpBfhwt68=",
        "password": "AmlbcKtg1WQsLuivLpjyOTVizNrijZiXY6vVJoT5a1U=",
        "store_id": "a504304c-ad27-4b9b-8625-92a314498e64",
    },
    "Jeep Apparel": {
        "username": "+w3K5hLq56MQ4ijqFH78lV0xQCTTzP9mNAqToCUL9Cw=",
        "password": "l2+ozGqsA6PX7MSHrl4OMwZRTieKzUpJVWv/WYye8iA=",
        "store_id": "80f123d6-f9de-45b9-938c-61c0a358f205",
    },
    "Superdry": {
        "username": "zcUrzwFh2QwtH1yEJixFXtUA4XGQyx2wbNVLpYTzZ8M=",
        "password": "92Av8tHsbq2XLEZZeRwYNsPFSkca+dD1cwRQs79rooM=",
        "store_id": "b112948b-0390-4833-8f41-47f997c5382c",
    },
    "Reebok": {
        "username": "9oZ10dMWlyQpEmS0Kw6xhIcKYXw8lB2az3Q0Zb+KBAw=",
        "password": "Cq/Zn86P7FT3EN0C5qzOewAQssyvrDSbkzmQBSAOrMY=",
        "store_id": "963f57af-6f46-4d6d-b07c-dc4aa684cdfa",
    },
    "Levi's": {
        "username": "4IQbm0CgLBZQkzliPnwWnjCQqgEdXsP6mVQ6q7nX24Y=",
        "password": "70JK3u4z/lxrGdpdSUE4csPfzlg/wgGTcuAgUgbd+j4=",
        "store_id": "ea344f50-5af3-4de1-814c-0c45171a2353",
    },
}

BASE_URL = "https://storeapi.parcelninja.com/api/v1"
PAGE_SIZE = 50
MAX_PAGES = 20  # Safety cap — 20 pages × 50 = 1000 records

INBOUND_STATUS_MAP = {
    200: "Awaiting Arrival",
    201: "Arrived at Warehouse",
    202: "Being Processed",
    203: "Processing Complete",
    204: "Complete with Variance",
}

# ─────────────────────────────────────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _auth(store: dict):
    return HTTPBasicAuth(store["username"], store["password"])

def _headers(store: dict):
    return {"Accept": "application/json", "X-Store-Id": store["store_id"]}

def _get(url, store, params=None, timeout=15):
    try:
        r = requests.get(url, auth=_auth(store), headers=_headers(store),
                         params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        return None


@st.cache_data(ttl=600, show_spinner=False)
def get_inventory_name(sku: str, store_name: str) -> str:
    """Cached SKU → descriptive name lookup."""
    store = STORES[store_name]
    data = _get(f"{BASE_URL}/inventory/{sku}", store)
    if data:
        items = data.get("items", [])
        if items:
            return items[0].get("name") or sku
    return sku


def fetch_outbound_detail(outbound_id: str, store: dict) -> dict | None:
    return _get(f"{BASE_URL}/outbounds/{outbound_id}/events", store)


def fetch_inbound_detail(inbound_id: str, store: dict) -> dict | None:
    return _get(f"{BASE_URL}/inbounds/{inbound_id}/events", store)


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def search_outbounds(store_name: str, query: str, start_date: datetime, end_date: datetime) -> list[dict]:
    """
    Paginate through outbounds and match records where:
      - channelId contains query  (Shopify order ref, e.g. D20388)
      - clientId contains query   (internal ref)
      - id contains query         (PNJ ID)
    Returns list of fully-detailed event dicts tagged with store_name.
    """
    store = STORES[store_name]
    q = query.strip().upper()
    results = []
    sd = start_date.strftime("%Y%m%d")
    ed = end_date.strftime("%Y%m%d")

    for page in range(1, MAX_PAGES + 1):
        params = {
            "pageSize": PAGE_SIZE,
            "page": page,
            "startDate": sd,
            "endDate": ed,
            "orderBy": "createDate",
            "orderDirection": "desc",
        }
        data = _get(f"{BASE_URL}/outbounds", store, params)
        if not data:
            break

        records = data.get("outbounds", [])
        if not records:
            break

        for summary in records:
            s_id = str(summary.get("id", ""))
            s_channel = str(summary.get("channelId") or "").upper()
            s_client = str(summary.get("clientId") or "").upper()

            if q in s_id or q in s_channel or q in s_client:
                detail = fetch_outbound_detail(s_id, store)
                if detail:
                    detail["_store"] = store_name
                    detail["_type"] = "outbound"
                    results.append(detail)

        # If the oldest record on this page predates start_date, stop
        if len(records) < PAGE_SIZE:
            break

    return results


def search_inbounds(store_name: str, query: str, start_date: datetime, end_date: datetime) -> list[dict]:
    """
    Paginate through inbounds and match records where:
      - channelId, clientId, supplierReference, id contain query
    Also used for SHP→RET linking (pass clientId of the outbound).
    """
    store = STORES[store_name]
    q = query.strip().upper()
    results = []
    sd = start_date.strftime("%Y%m%d")
    ed = end_date.strftime("%Y%m%d")

    for page in range(1, MAX_PAGES + 1):
        params = {
            "pageSize": PAGE_SIZE,
            "page": page,
            "startDate": sd,
            "endDate": ed,
            "col": 4,
            "colOrder": "desc",
        }
        data = _get(f"{BASE_URL}/inbounds", store, params)
        if not data:
            break

        records = data.get("inbounds", [])
        if not records:
            break

        for summary in records:
            s_id = str(summary.get("id", ""))
            s_channel = str(summary.get("channelId") or "").upper()
            s_client = str(summary.get("clientId") or "").upper()
            s_supplier = str(summary.get("supplierReference") or "").upper()

            if q in s_id or q in s_channel or q in s_client or q in s_supplier:
                detail = fetch_inbound_detail(s_id, store)
                if detail:
                    detail["_store"] = store_name
                    detail["_type"] = "inbound"
                    results.append(detail)

        if len(records) < PAGE_SIZE:
            break

    return results


def search_all_stores(
    query: str,
    search_outbounds_flag: bool,
    search_inbounds_flag: bool,
    selected_stores: list[str],
    start_date: datetime,
    end_date: datetime,
) -> tuple[list[dict], list[dict]]:
    """Run search across selected stores in parallel."""
    outbound_results = []
    inbound_results = []

    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        futures_out = {}
        futures_in = {}

        for store_name in selected_stores:
            if search_outbounds_flag:
                f = executor.submit(search_outbounds, store_name, query, start_date, end_date)
                futures_out[f] = store_name
            if search_inbounds_flag:
                f = executor.submit(search_inbounds, store_name, query, start_date, end_date)
                futures_in[f] = store_name

        for f in concurrent.futures.as_completed(futures_out):
            outbound_results.extend(f.result())

        for f in concurrent.futures.as_completed(futures_in):
            inbound_results.extend(f.result())

    return outbound_results, inbound_results


def find_linked_returns(outbound: dict, start_date: datetime, end_date: datetime) -> list[dict]:
    """
    Given an outbound, search inbounds across the same store for any record
    whose clientId or supplierReference matches the outbound's clientId or channelId.
    This links SHP → RET.
    """
    store_name = outbound.get("_store")
    if not store_name:
        return []

    linked = []
    for ref in [outbound.get("clientId"), outbound.get("channelId")]:
        if ref:
            hits = search_inbounds(store_name, str(ref), start_date, end_date)
            for h in hits:
                if h.get("id") not in [r.get("id") for r in linked]:
                    linked.append(h)
    return linked


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

STATUS_COLOURS = {
    "delivered": "🟢",
    "dispatched": "🔵",
    "collected": "🔵",
    "processing": "🟡",
    "cancelled": "🔴",
    "returned": "🟠",
    "awaiting": "⚪",
    "arrived": "🟡",
    "complete": "🟢",
    "variance": "🟠",
}

def status_icon(description: str) -> str:
    d = (description or "").lower()
    for key, icon in STATUS_COLOURS.items():
        if key in d:
            return icon
    return "⚫"


def render_outbound_card(ob: dict, show_linked_returns: bool, start_date, end_date):
    store_name = ob.get("_store", "Unknown")
    channel_id = ob.get("channelId") or "—"
    client_id = ob.get("clientId") or "—"
    pnj_id = ob.get("id") or "—"
    status_desc = ob.get("status", {}).get("description") or "Unknown"
    status_code = ob.get("status", {}).get("code") or ""
    d_info = ob.get("deliveryInfo", {})
    items = ob.get("items", [])
    events = ob.get("events", [])
    create_date = ob.get("createDate", "")

    icon = status_icon(status_desc)

    with st.container():
        st.markdown(
            f"""
            <div style="border:1px solid #2d4a6e; border-radius:8px; padding:16px; margin-bottom:12px; background:#0d1b2e;">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px;">
                    <div>
                        <span style="font-size:1.1rem; font-weight:700; color:#e8f0fe;">📦 {channel_id}</span>
                        <span style="margin-left:12px; color:#8ab4f8; font-size:0.85rem;">{store_name}</span>
                    </div>
                    <div style="text-align:right;">
                        <span style="font-size:0.9rem;">{icon} <strong style="color:#e8f0fe;">{status_desc}</strong></span>
                        <span style="color:#5f6368; font-size:0.8rem; margin-left:8px;">({status_code})</span>
                    </div>
                </div>
                <div style="margin-top:8px; display:flex; gap:24px; flex-wrap:wrap; color:#9aa0a6; font-size:0.82rem;">
                    <span><strong style="color:#8ab4f8;">PNJ ID:</strong> {pnj_id}</span>
                    <span><strong style="color:#8ab4f8;">Client Ref:</strong> {client_id}</span>
                    <span><strong style="color:#8ab4f8;">Created:</strong> {create_date[:10] if create_date else '—'}</span>
                    <span><strong style="color:#8ab4f8;">Courier:</strong> {d_info.get('courierName') or '—'}</span>
                    <span><strong style="color:#8ab4f8;">Waybill:</strong> {d_info.get('trackingNo') or '—'}</span>
                </div>
                <div style="margin-top:6px; color:#9aa0a6; font-size:0.82rem;">
                    <strong style="color:#8ab4f8;">Customer:</strong> {d_info.get('customer') or '—'} &nbsp;|&nbsp;
                    <strong style="color:#8ab4f8;">Address:</strong> {', '.join([str(p) for p in [d_info.get('addressLine1'), d_info.get('suburb'), d_info.get('postalCode')] if p]) or '—'}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.expander(f"📋 Items ({len(items)}) · Events ({len(events)})"):
            col1, col2 = st.columns(2)

            with col1:
                st.markdown("**Items**")
                for item in items:
                    sku = item.get("itemNo") or "N/A"
                    qty = item.get("qty") or 0
                    name = get_inventory_name(sku, store_name) if sku != "N/A" else "N/A"
                    ret_reason = item.get("returnReason")
                    serials = item.get("SerialNumbers", [])

                    st.markdown(f"- **{sku}** — {name}  \n  Qty: `{qty}`" +
                                (f"  \n  ⚠️ Return: {ret_reason}" if ret_reason else "") +
                                (f"  \n  S/N: {', '.join(serials)}" if serials else ""))

            with col2:
                st.markdown("**Event Timeline**")
                for ev in events:
                    ev_date = str(ev.get("date") or ev.get("createDate") or "")[:16]
                    ev_desc = ev.get("description") or ev.get("statusDescription") or str(ev)
                    st.markdown(f"- `{ev_date}` — {ev_desc}")

            # Raw data toggle
            if st.checkbox(f"Show raw JSON", key=f"raw_ob_{pnj_id}"):
                st.json(ob)

        # SHP → RET linking
        if show_linked_returns:
            with st.spinner("Searching for linked returns..."):
                linked = find_linked_returns(ob, start_date, end_date)
            if linked:
                st.markdown(
                    f"<div style='border-left:3px solid #f28b82; padding-left:12px; margin-top:-4px;'>"
                    f"<span style='color:#f28b82; font-weight:700;'>↩ {len(linked)} Linked Return(s) Found</span></div>",
                    unsafe_allow_html=True,
                )
                for ret in linked:
                    render_inbound_card(ret)
            else:
                st.caption("↩ No linked returns found for this shipment.")


def render_inbound_card(ib: dict):
    store_name = ib.get("_store", "Unknown")
    channel_id = ib.get("channelId") or "—"
    client_id = ib.get("clientId") or "—"
    pnj_id = ib.get("id") or "—"
    supplier_ref = ib.get("supplierReference") or "—"
    inbound_type = ib.get("type", {}).get("description") or "—"
    events = ib.get("events", [])
    items = ib.get("items", [])
    create_date = ib.get("createDate", "")

    # Resolve status from most recent event
    latest_status = "Unknown"
    if events:
        latest_status = events[0].get("description") or events[0].get("statusDescription") or "Unknown"

    icon = status_icon(latest_status)
    d_info = ib.get("deliveryInfo", {})
    p_info = ib.get("pickupInfo", {})
    customer = d_info.get("customer") or p_info.get("recipient") or "—"

    with st.container():
        st.markdown(
            f"""
            <div style="border:1px solid #4a2d2d; border-radius:8px; padding:16px; margin-bottom:12px; background:#1e0d0d;">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px;">
                    <div>
                        <span style="font-size:1.1rem; font-weight:700; color:#f8e8e8;">↩ RETURN/INBOUND</span>
                        <span style="margin-left:12px; color:#f28b82; font-size:0.85rem;">{store_name} · {inbound_type}</span>
                    </div>
                    <div>
                        <span style="font-size:0.9rem;">{icon} <strong style="color:#f8e8e8;">{latest_status}</strong></span>
                    </div>
                </div>
                <div style="margin-top:8px; display:flex; gap:24px; flex-wrap:wrap; color:#9aa0a6; font-size:0.82rem;">
                    <span><strong style="color:#f28b82;">Channel ID:</strong> {channel_id}</span>
                    <span><strong style="color:#f28b82;">PNJ ID:</strong> {pnj_id}</span>
                    <span><strong style="color:#f28b82;">Client Ref:</strong> {client_id}</span>
                    <span><strong style="color:#f28b82;">Supplier Ref:</strong> {supplier_ref}</span>
                    <span><strong style="color:#f28b82;">Created:</strong> {create_date[:10] if create_date else '—'}</span>
                    <span><strong style="color:#f28b82;">Sender:</strong> {customer}</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.expander(f"📋 Items ({len(items)}) · Events ({len(events)})"):
            col1, col2 = st.columns(2)

            with col1:
                st.markdown("**Items**")
                for item in items:
                    sku = item.get("itemNo") or "N/A"
                    expected = item.get("qty") or 0
                    received = item.get("receivedQty") or 0
                    name = item.get("name") or sku
                    variance = ""
                    if received and received != expected:
                        variance = f"  \n  ⚠️ Variance: expected {expected}, received {received}"
                    st.markdown(f"- **{sku}** — {name}  \n  Qty: `{expected}`{variance}")

            with col2:
                st.markdown("**Event Timeline**")
                for ev in events:
                    ev_date = str(ev.get("date") or ev.get("createDate") or "")[:16]
                    ev_desc = ev.get("description") or ev.get("statusDescription") or str(ev)
                    st.markdown(f"- `{ev_date}` — {ev_desc}")

            if st.checkbox("Show raw JSON", key=f"raw_ib_{pnj_id}"):
                st.json(ib)


# ─────────────────────────────────────────────────────────────────────────────
# STREAMLIT UI
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Parcelninja Search",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=DM+Sans:wght@300;400;600;700&display=swap');

html, body, [class*="css"] {
    font-family: 'DM Sans', sans-serif;
    background-color: #060e1a;
    color: #c8d3e8;
}

.stApp { background-color: #060e1a; }

h1, h2, h3 {
    font-family: 'IBM Plex Mono', monospace;
    letter-spacing: -0.5px;
}

.stTextInput input {
    background: #0d1b2e;
    color: #e8f0fe;
    border: 1px solid #2d4a6e;
    border-radius: 6px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1rem;
}

.stButton > button {
    background: linear-gradient(135deg, #1a73e8, #0d47a1);
    color: white;
    border: none;
    border-radius: 6px;
    font-family: 'DM Sans', sans-serif;
    font-weight: 600;
    padding: 0.5rem 1.5rem;
    width: 100%;
}

.stButton > button:hover {
    background: linear-gradient(135deg, #4285f4, #1a73e8);
}

.stMultiSelect > div, .stSelectbox > div {
    background: #0d1b2e;
    border-color: #2d4a6e;
}

.stDateInput input {
    background: #0d1b2e;
    border-color: #2d4a6e;
    color: #e8f0fe;
}

[data-testid="stSidebar"] {
    background: #08121f;
    border-right: 1px solid #1a2e4a;
}

.stExpander {
    border: 1px solid #1a2e4a !important;
    border-radius: 6px !important;
    background: #08121f !important;
}

.metric-card {
    background: #0d1b2e;
    border: 1px solid #2d4a6e;
    border-radius: 8px;
    padding: 16px;
    text-align: center;
}

div[data-testid="stMetric"] {
    background: #0d1b2e;
    border: 1px solid #1a2e4a;
    border-radius: 8px;
    padding: 12px 16px;
}

.search-hint {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.75rem;
    color: #5f7a9a;
    margin-top: 4px;
}

.tab-header {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.8rem;
    color: #8ab4f8;
    text-transform: uppercase;
    letter-spacing: 1px;
}
</style>
""", unsafe_allow_html=True)

# ── Header ──────────────────────────────────────────────────────────────────
st.markdown("""
<div style="padding: 24px 0 12px 0; border-bottom: 1px solid #1a2e4a; margin-bottom: 24px;">
    <div style="display:flex; align-items:baseline; gap:12px;">
        <span style="font-family:'IBM Plex Mono',monospace; font-size:1.6rem; font-weight:700; color:#e8f0fe;">📦 Parcelninja</span>
        <span style="font-family:'IBM Plex Mono',monospace; font-size:1.6rem; color:#3c6db0;">/ Search</span>
    </div>
    <div style="color:#5f7a9a; font-size:0.85rem; margin-top:4px;">
        Multi-store shipment & returns lookup · Bounty Brands Hub
    </div>
</div>
""", unsafe_allow_html=True)

# ── Sidebar: Filters ─────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙️ Search Config")
    st.markdown("---")

    selected_stores = st.multiselect(
        "Stores",
        options=list(STORES.keys()),
        default=list(STORES.keys()),
        help="Search across selected stores simultaneously",
    )

    st.markdown("**Date Range**")
    col_a, col_b = st.columns(2)
    with col_a:
        start_date = st.date_input("From", value=datetime.now() - timedelta(days=180),
                                   label_visibility="collapsed")
    with col_b:
        end_date = st.date_input("To", value=datetime.now(),
                                 label_visibility="collapsed")

    st.markdown("---")
    st.markdown("**Search In**")
    search_outbounds_flag = st.checkbox("Outbounds (Shipments)", value=True)
    search_inbounds_flag = st.checkbox("Inbounds (Returns / Stock)", value=True)
    show_linked_returns = st.checkbox("Auto-link Returns to Shipments", value=True,
                                      help="For each matched shipment, automatically search for linked return inbounds")

    st.markdown("---")
    st.markdown("""
    <div style="color:#5f7a9a; font-size:0.78rem; line-height:1.6;">
        <strong style="color:#8ab4f8;">Search matches against:</strong><br>
        • Channel ID (Shopify ref, e.g. <code>D20388</code>)<br>
        • Client Ref (internal ERP ref)<br>
        • PNJ ID (warehouse ID)<br>
        • Supplier Ref (inbounds only)<br><br>
        Results capped at 1,000 records per store per search type.
    </div>
    """, unsafe_allow_html=True)

# ── Main Search Bar ──────────────────────────────────────────────────────────
search_col, btn_col = st.columns([5, 1])
with search_col:
    query = st.text_input(
        "Search Query",
        placeholder="e.g.  D20388  ·  SHP-1234  ·  PNJ-ID  ·  waybill  ·  client ref",
        label_visibility="collapsed",
    )
    st.markdown('<div class="search-hint">Enter Shopify order number, PNJ ID, client ref, or any reference field</div>',
                unsafe_allow_html=True)

with btn_col:
    st.markdown("<br>", unsafe_allow_html=True)
    search_clicked = st.button("🔍 Search", use_container_width=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── Search Execution ─────────────────────────────────────────────────────────
if search_clicked and query.strip():
    if not selected_stores:
        st.warning("Select at least one store.")
        st.stop()

    if not search_outbounds_flag and not search_inbounds_flag:
        st.warning("Enable at least one search type (Outbounds / Inbounds).")
        st.stop()

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    with st.spinner(f"Searching {len(selected_stores)} store(s) for **{query}** ..."):
        outbounds, inbounds = search_all_stores(
            query=query,
            search_outbounds_flag=search_outbounds_flag,
            search_inbounds_flag=search_inbounds_flag,
            selected_stores=selected_stores,
            start_date=start_dt,
            end_date=end_dt,
        )

    total = len(outbounds) + len(inbounds)

    if total == 0:
        st.info(f"No results found for **{query}** across the selected stores and date range. "
                f"Try widening the date range or checking the reference.")
    else:
        # Summary metrics
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Results", total)
        m2.metric("Shipments (Out)", len(outbounds))
        m3.metric("Inbounds / Returns", len(inbounds))
        m4.metric("Stores Searched", len(selected_stores))

        st.markdown("---")

        # Tabs: Outbounds | Inbounds | All
        if outbounds and inbounds:
            tab_out, tab_in = st.tabs([
                f"📦 Outbounds ({len(outbounds)})",
                f"↩ Inbounds / Returns ({len(inbounds)})",
            ])
        elif outbounds:
            tab_out = st.container()
            tab_in = None
        else:
            tab_out = None
            tab_in = st.container()

        if outbounds and tab_out:
            with tab_out:
                st.markdown(f"<div class='tab-header'>Shipments Matching · {query}</div>",
                            unsafe_allow_html=True)
                st.markdown("<br>", unsafe_allow_html=True)
                for ob in outbounds:
                    render_outbound_card(ob, show_linked_returns, start_dt, end_dt)

        if inbounds and tab_in:
            with tab_in:
                st.markdown(f"<div class='tab-header'>Inbounds / Returns Matching · {query}</div>",
                            unsafe_allow_html=True)
                st.markdown("<br>", unsafe_allow_html=True)
                for ib in inbounds:
                    render_inbound_card(ib)

elif search_clicked and not query.strip():
    st.warning("Enter a search term.")

else:
    # Landing state
    st.markdown("""
    <div style="text-align:center; padding:60px 0; color:#3c6db0;">
        <div style="font-size:3rem;">📦</div>
        <div style="font-family:'IBM Plex Mono',monospace; font-size:1rem; margin-top:12px; color:#5f7a9a;">
            Enter a reference to search across all stores
        </div>
        <div style="font-size:0.82rem; margin-top:8px; color:#3c5070;">
            Channel ID (Shopify) · Client Ref · PNJ ID · Supplier Ref · Waybill
        </div>
    </div>
    """, unsafe_allow_html=True)
