"""
Parcelninja Multi-Store Search — Production Build
Bounty Brands + Levi's Warehouse Hub

Improvements over v1:
  - Per-request retry logic with exponential backoff (429/503/network)
  - Errors surfaced to UI — no more silent None returns
  - find_linked_returns() pre-fetched in parallel, NOT inside render loop
  - st.rerun() replaces deprecated st.experimental_rerun()
  - Structured logging (stderr) + st.warning() for operational visibility
  - Strict type hints throughout
  - Credential discrepancy guard at startup
  - Search scoped to date window before client-side match (unchanged API constraint — documented)
  - MAX_PAGES safety cap with user-visible warning when hit
  - Empty-response vs error-response distinction surfaced in UI
  - All store configs loaded once; never re-read secrets per-request
"""

from __future__ import annotations

import concurrent.futures
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests
import streamlit as st
from requests.auth import HTTPBasicAuth
from requests.exceptions import ConnectionError, ReadTimeout, RequestException

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("pnj_search")


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL   = "https://storeapi.parcelninja.com/api/v1"
PAGE_SIZE  = 50
MAX_PAGES  = 40          # 40 × 50 = 2,000 records per store per search type
MAX_WORKERS = 12         # ThreadPoolExecutor ceiling
REQUEST_TIMEOUT = 15     # seconds per HTTP call
RETRY_ATTEMPTS  = 3      # attempts before giving up
RETRY_BACKOFF   = [1, 2, 4]  # seconds between retries

INBOUND_STATUS_MAP: dict[int, str] = {
    200: "Awaiting Arrival",
    201: "Arrived at Warehouse",
    202: "Being Processed",
    203: "Processing Complete",
    204: "Complete with Variance",
}

STATUS_ICONS: dict[str, str] = {
    "delivered":  "🟢",
    "dispatched": "🔵",
    "collected":  "🔵",
    "processing": "🟡",
    "cancelled":  "🔴",
    "returned":   "🟠",
    "awaiting":   "⚪",
    "arrived":    "🟡",
    "complete":   "🟢",
    "variance":   "🟠",
}


# ─────────────────────────────────────────────────────────────────────────────
# STORE CONFIG
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StoreConfig:
    name:     str
    username: str
    password: str
    store_id: str

    def auth(self) -> HTTPBasicAuth:
        return HTTPBasicAuth(self.username, self.password)

    def headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "X-Store-Id": self.store_id}


@st.cache_resource
def load_stores() -> dict[str, StoreConfig]:
    """
    Load store credentials from st.secrets exactly once.
    Raises a clear RuntimeError if any required key is missing —
    fail fast rather than silently return broken configs.
    """
    required_keys = ["diesel", "hurley", "jeep_apparel", "superdry", "reebok", "levis"]
    missing = [k for k in required_keys if k not in st.secrets]
    if missing:
        raise RuntimeError(
            f"Missing secrets for: {missing}. "
            "Add them to .streamlit/secrets.toml before running."
        )

    def _store(key: str, display_name: str) -> StoreConfig:
        s = st.secrets[key]
        return StoreConfig(
            name=display_name,
            username=s["username"],
            password=s["password"],
            store_id=s["store_id"],
        )

    return {
        "Diesel":       _store("diesel",       "Diesel"),
        "Hurley":       _store("hurley",       "Hurley"),
        "Jeep Apparel": _store("jeep_apparel", "Jeep Apparel"),
        "Superdry":     _store("superdry",     "Superdry"),
        "Reebok":       _store("reebok",       "Reebok"),
        "Levi's":       _store("levis",        "Levi's"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP LAYER — RETRY + ERROR SURFACING
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ApiResult:
    data:       Optional[dict]
    ok:         bool
    status_code: int
    error:      Optional[str] = None


def _get_with_retry(
    url: str,
    store: StoreConfig,
    params: Optional[dict] = None,
) -> ApiResult:
    """
    GET with retry + exponential backoff.
    Returns ApiResult — never raises, never silently returns None.

    Retry on: 429 (rate limit), 503 (unavailable), connection errors, timeouts.
    Do not retry on: 4xx auth/not-found errors (would just fail again).
    """
    last_error: Optional[str] = None

    for attempt, delay in enumerate(RETRY_BACKOFF):
        try:
            r = requests.get(
                url,
                auth=store.auth(),
                headers=store.headers(),
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if r.status_code == 200:
                return ApiResult(data=r.json(), ok=True, status_code=200)

            if r.status_code in (429, 503):
                # Transient — back off and retry
                retry_after = int(r.headers.get("Retry-After", delay))
                log.warning(
                    "Store=%s HTTP %s on %s — backing off %ss (attempt %d/%d)",
                    store.name, r.status_code, url, retry_after,
                    attempt + 1, RETRY_ATTEMPTS,
                )
                if attempt < RETRY_ATTEMPTS - 1:
                    time.sleep(retry_after)
                    continue

            # Non-retryable HTTP error
            msg = f"HTTP {r.status_code} from {url}"
            log.error("Store=%s %s — body: %s", store.name, msg, r.text[:200])
            return ApiResult(data=None, ok=False, status_code=r.status_code, error=msg)

        except ReadTimeout:
            last_error = f"Timeout after {REQUEST_TIMEOUT}s — {url}"
            log.warning("Store=%s %s (attempt %d/%d)", store.name, last_error, attempt + 1, RETRY_ATTEMPTS)
        except ConnectionError as e:
            last_error = f"Connection error — {url}: {e}"
            log.warning("Store=%s %s (attempt %d/%d)", store.name, last_error, attempt + 1, RETRY_ATTEMPTS)
        except RequestException as e:
            last_error = f"Request failed — {url}: {e}"
            log.error("Store=%s %s", store.name, last_error)
            break  # Non-transient

        if attempt < RETRY_ATTEMPTS - 1:
            time.sleep(delay)

    return ApiResult(data=None, ok=False, status_code=0, error=last_error or "Unknown error")


# ─────────────────────────────────────────────────────────────────────────────
# INVENTORY LOOKUP — CACHED
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=600, show_spinner=False)
def get_inventory_name(sku: str, store_name: str) -> str:
    """
    Cached SKU → descriptive name.
    Falls back to SKU string on any failure — never raises.
    Cache is keyed on (sku, store_name) since SKUs may differ across stores.
    TTL: 10 minutes.
    """
    if not sku or sku == "N/A":
        return "Unknown"

    stores = load_stores()
    store  = stores.get(store_name)
    if not store:
        return sku

    result = _get_with_retry(f"{BASE_URL}/inventory/{sku}", store)
    if result.ok and result.data:
        items = result.data.get("items", [])
        if items:
            return items[0].get("name") or sku

    return sku


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SearchOutcome:
    records:      list[dict]
    capped:       bool   = False   # True if MAX_PAGES was hit
    errors:       list[str] = None # API errors encountered

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def search_outbounds(
    store_name: str,
    query: str,
    start_date: datetime,
    end_date: datetime,
) -> SearchOutcome:
    """
    Paginate /outbounds and match client-side against:
      channelId, clientId, id, deliveryInfo.trackingNo
    On match, fetches full /outbounds/{id}/events detail.
    Returns SearchOutcome with results, cap flag, and any errors.
    """
    stores = load_stores()
    store  = stores[store_name]
    q      = query.strip().upper()
    results: list[dict] = []
    errors:  list[str]  = []
    capped = False
    sd = start_date.strftime("%Y%m%d")
    ed = end_date.strftime("%Y%m%d")

    for page in range(1, MAX_PAGES + 1):
        result = _get_with_retry(
            f"{BASE_URL}/outbounds",
            store,
            params={
                "pageSize":       PAGE_SIZE,
                "page":           page,
                "startDate":      sd,
                "endDate":        ed,
                "orderBy":        "createDate",
                "orderDirection": "desc",
            },
        )

        if not result.ok:
            errors.append(f"{store_name}/outbounds p{page}: {result.error}")
            break

        records = result.data.get("outbounds", [])
        if not records:
            break

        for summary in records:
            s_id     = str(summary.get("id", ""))
            s_ch     = str(summary.get("channelId") or "").upper()
            s_cl     = str(summary.get("clientId")  or "").upper()
            s_way    = str((summary.get("deliveryInfo") or {}).get("trackingNo") or "").upper()

            if q in s_id or q in s_ch or q in s_cl or q in s_way:
                detail = _get_with_retry(f"{BASE_URL}/outbounds/{s_id}/events", store)
                if detail.ok and detail.data:
                    detail.data["_store"] = store_name
                    detail.data["_type"]  = "outbound"
                    results.append(detail.data)
                elif not detail.ok:
                    errors.append(f"{store_name}/outbounds/{s_id}/events: {detail.error}")

        if len(records) < PAGE_SIZE:
            break

        if page == MAX_PAGES:
            capped = True
            log.warning("Store=%s outbounds search hit MAX_PAGES cap (%d)", store_name, MAX_PAGES)

    return SearchOutcome(records=results, capped=capped, errors=errors)


def search_inbounds(
    store_name: str,
    query: str,
    start_date: datetime,
    end_date: datetime,
) -> SearchOutcome:
    """
    Paginate /inbounds and match client-side against:
      channelId, clientId, supplierReference, id, customer name
    On match, fetches full /inbounds/{id}/events detail.
    Also used internally for SHP→RET linking.
    """
    stores = load_stores()
    store  = stores[store_name]
    q      = query.strip().upper()
    results: list[dict] = []
    errors:  list[str]  = []
    capped = False
    sd = start_date.strftime("%Y%m%d")
    ed = end_date.strftime("%Y%m%d")

    for page in range(1, MAX_PAGES + 1):
        result = _get_with_retry(
            f"{BASE_URL}/inbounds",
            store,
            params={
                "pageSize": PAGE_SIZE,
                "page":     page,
                "startDate": sd,
                "endDate":   ed,
                "col":       4,
                "colOrder":  "desc",
            },
        )

        if not result.ok:
            errors.append(f"{store_name}/inbounds p{page}: {result.error}")
            break

        records = result.data.get("inbounds", [])
        if not records:
            break

        for summary in records:
            s_id      = str(summary.get("id", ""))
            s_ch      = str(summary.get("channelId")         or "").upper()
            s_cl      = str(summary.get("clientId")          or "").upper()
            s_sup     = str(summary.get("supplierReference") or "").upper()
            d_info    = summary.get("deliveryInfo", {})
            p_info    = summary.get("pickupInfo",   {})
            s_cust    = str(d_info.get("customer") or p_info.get("recipient") or "").upper()

            if q in s_id or q in s_ch or q in s_cl or q in s_sup or q in s_cust:
                detail = _get_with_retry(f"{BASE_URL}/inbounds/{s_id}/events", store)
                if detail.ok and detail.data:
                    detail.data["_store"] = store_name
                    detail.data["_type"]  = "inbound"
                    results.append(detail.data)
                elif not detail.ok:
                    errors.append(f"{store_name}/inbounds/{s_id}/events: {detail.error}")

        if len(records) < PAGE_SIZE:
            break

        if page == MAX_PAGES:
            capped = True
            log.warning("Store=%s inbounds search hit MAX_PAGES cap (%d)", store_name, MAX_PAGES)

    return SearchOutcome(records=results, capped=capped, errors=errors)


def _fetch_linked_returns_for(
    outbound: dict,
    start_date: datetime,
    end_date: datetime,
) -> list[dict]:
    """
    Search inbounds for records whose clientId or supplierReference
    matches the outbound's clientId or channelId.
    Deduplicates by PNJ id.
    """
    store_name = outbound.get("_store")
    if not store_name:
        return []

    seen: set[str] = set()
    linked: list[dict] = []

    for ref in filter(None, [outbound.get("clientId"), outbound.get("channelId")]):
        outcome = search_inbounds(store_name, str(ref), start_date, end_date)
        for h in outcome.records:
            h_id = str(h.get("id", ""))
            if h_id not in seen:
                seen.add(h_id)
                linked.append(h)

    return linked


def run_search(
    query: str,
    selected_stores: list[str],
    search_outbounds_flag: bool,
    search_inbounds_flag: bool,
    link_returns: bool,
    start_date: datetime,
    end_date: datetime,
) -> tuple[list[dict], list[dict], list[str], bool]:
    """
    Fan out across all selected stores in parallel.
    Pre-fetches linked returns before returning — NOT in the render loop.

    Returns: (outbounds, inbounds, all_errors, any_capped)
    """
    all_outbounds: list[dict]  = []
    all_inbounds:  list[dict]  = []
    all_errors:    list[str]   = []
    any_capped                 = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fut_out: dict[concurrent.futures.Future, str] = {}
        fut_in:  dict[concurrent.futures.Future, str] = {}

        for sn in selected_stores:
            if search_outbounds_flag:
                fut_out[ex.submit(search_outbounds, sn, query, start_date, end_date)] = sn
            if search_inbounds_flag:
                fut_in[ex.submit(search_inbounds, sn, query, start_date, end_date)] = sn

        for f in concurrent.futures.as_completed(fut_out):
            outcome: SearchOutcome = f.result()
            all_outbounds.extend(outcome.records)
            all_errors.extend(outcome.errors)
            if outcome.capped:
                any_capped = True

        for f in concurrent.futures.as_completed(fut_in):
            outcome: SearchOutcome = f.result()
            all_inbounds.extend(outcome.records)
            all_errors.extend(outcome.errors)
            if outcome.capped:
                any_capped = True

    # Pre-fetch linked returns for all matched outbounds — parallel, outside render
    if link_returns and all_outbounds:
        linked_map: dict[str, list[dict]] = {}
        link_futures: dict[concurrent.futures.Future, str] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for ob in all_outbounds:
                ob_id = str(ob.get("id", ""))
                f = ex.submit(_fetch_linked_returns_for, ob, start_date, end_date)
                link_futures[f] = ob_id

            for f in concurrent.futures.as_completed(link_futures):
                ob_id = link_futures[f]
                linked_map[ob_id] = f.result()

        # Attach linked returns directly to outbound record
        for ob in all_outbounds:
            ob["_linked_returns"] = linked_map.get(str(ob.get("id", "")), [])
    else:
        for ob in all_outbounds:
            ob["_linked_returns"] = []

    return all_outbounds, all_inbounds, all_errors, any_capped


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _status_icon(description: str) -> str:
    d = (description or "").lower()
    for key, icon in STATUS_ICONS.items():
        if key in d:
            return icon
    return "⚫"


def _addr(info: dict) -> str:
    parts = [info.get("addressLine1"), info.get("addressLine2"),
             info.get("suburb"), info.get("postalCode")]
    return ", ".join(str(p) for p in parts if p) or "—"


def render_outbound_card(ob: dict, show_linked_returns: bool) -> None:
    store_name   = ob.get("_store", "Unknown")
    channel_id   = ob.get("channelId")   or "—"
    client_id    = ob.get("clientId")    or "—"
    pnj_id       = ob.get("id")          or "—"
    status       = ob.get("status", {})
    status_desc  = status.get("description") or "Unknown"
    status_code  = status.get("code")        or ""
    d_info       = ob.get("deliveryInfo", {})
    items        = ob.get("items", [])
    events       = ob.get("events", [])
    create_date  = (ob.get("createDate") or "")[:10] or "—"
    icon         = _status_icon(status_desc)

    st.markdown(
        f"""
        <div class="card card-out">
            <div class="card-row card-header-row">
                <div>
                    <span class="card-title">📦 {channel_id}</span>
                    <span class="card-tag">{store_name}</span>
                </div>
                <div class="card-status">
                    {icon} <strong>{status_desc}</strong>
                    <span class="status-code">({status_code})</span>
                </div>
            </div>
            <div class="card-row card-meta">
                <span><b>PNJ ID</b> {pnj_id}</span>
                <span><b>Client Ref</b> {client_id}</span>
                <span><b>Created</b> {create_date}</span>
                <span><b>Courier</b> {d_info.get('courierName') or '—'}</span>
                <span><b>Waybill</b> {d_info.get('trackingNo') or '—'}</span>
            </div>
            <div class="card-row card-addr">
                <b>Customer</b> {d_info.get('customer') or '—'}
                &nbsp;·&nbsp;
                <b>Address</b> {_addr(d_info)}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander(f"📋 Items ({len(items)}) · Events ({len(events)})"):
        col_items, col_events = st.columns(2)

        with col_items:
            st.markdown("**Items**")
            for item in items:
                sku        = item.get("itemNo") or "N/A"
                qty        = item.get("qty") or 0
                name       = get_inventory_name(sku, store_name) if sku != "N/A" else "N/A"
                ret_reason = item.get("returnReason")
                serials    = item.get("SerialNumbers") or []
                line = f"- **{sku}** — {name}  \n  Qty: `{qty}`"
                if ret_reason:
                    line += f"  \n  ⚠️ Return: {ret_reason}"
                if serials:
                    line += f"  \n  S/N: {', '.join(serials)}"
                st.markdown(line)

        with col_events:
            st.markdown("**Event Timeline**")
            for ev in events:
                ev_date = str(ev.get("date") or ev.get("createDate") or "")[:16]
                ev_desc = ev.get("description") or ev.get("statusDescription") or str(ev)
                st.markdown(f"- `{ev_date}` — {ev_desc}")

        if st.checkbox("Show raw JSON", key=f"raw_ob_{pnj_id}"):
            st.json(ob)

    # Linked returns — pre-fetched, no blocking call here
    if show_linked_returns:
        linked = ob.get("_linked_returns", [])
        if linked:
            st.markdown(
                f"<div class='linked-badge'>↩ {len(linked)} Linked Return(s)</div>",
                unsafe_allow_html=True,
            )
            for ret in linked:
                render_inbound_card(ret)
        else:
            st.caption("↩ No linked returns found for this shipment.")


def render_inbound_card(ib: dict) -> None:
    store_name    = ib.get("_store", "Unknown")
    channel_id    = ib.get("channelId")         or "—"
    client_id     = ib.get("clientId")          or "—"
    pnj_id        = ib.get("id")                or "—"
    supplier_ref  = ib.get("supplierReference") or "—"
    inbound_type  = (ib.get("type") or {}).get("description") or "—"
    events        = ib.get("events", [])
    items         = ib.get("items", [])
    create_date   = (ib.get("createDate") or "")[:10] or "—"
    d_info        = ib.get("deliveryInfo", {})
    p_info        = ib.get("pickupInfo", {})
    customer      = d_info.get("customer") or p_info.get("recipient") or "—"

    latest_status = "Unknown"
    if events:
        latest_status = (
            events[0].get("description")
            or events[0].get("statusDescription")
            or "Unknown"
        )
    icon = _status_icon(latest_status)

    st.markdown(
        f"""
        <div class="card card-in">
            <div class="card-row card-header-row">
                <div>
                    <span class="card-title card-title-in">↩ RETURN / INBOUND</span>
                    <span class="card-tag card-tag-in">{store_name} · {inbound_type}</span>
                </div>
                <div class="card-status">
                    {icon} <strong>{latest_status}</strong>
                </div>
            </div>
            <div class="card-row card-meta">
                <span><b>Channel ID</b> {channel_id}</span>
                <span><b>PNJ ID</b> {pnj_id}</span>
                <span><b>Client Ref</b> {client_id}</span>
                <span><b>Supplier Ref</b> {supplier_ref}</span>
                <span><b>Created</b> {create_date}</span>
                <span><b>Sender</b> {customer}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander(f"📋 Items ({len(items)}) · Events ({len(events)})"):
        col_items, col_events = st.columns(2)

        with col_items:
            st.markdown("**Items**")
            for item in items:
                sku      = item.get("itemNo") or "N/A"
                expected = item.get("qty") or 0
                received = item.get("receivedQty") or 0
                name     = item.get("name") or sku
                line     = f"- **{sku}** — {name}  \n  Qty: `{expected}`"
                if received and received != expected:
                    line += f"  \n  ⚠️ Variance: expected {expected}, received {received}"
                st.markdown(line)

        with col_events:
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
    page_title="Parcelninja · Search",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:ital,wght@0,400;0,600;1,400&family=DM+Sans:wght@300;400;500;600;700&display=swap');

/* ── Base ── */
html, body, [class*="css"] {
    font-family: 'DM Sans', sans-serif;
    background-color: #050c18;
    color: #c4cfea;
}
.stApp { background-color: #050c18; }

/* ── Typography ── */
h1, h2, h3, h4, h5 {
    font-family: 'IBM Plex Mono', monospace;
    letter-spacing: -0.4px;
    color: #e8f0fe;
}

/* ── Inputs ── */
.stTextInput input {
    background: #0a1628;
    color: #e8f0fe;
    border: 1px solid #1e3a5f;
    border-radius: 6px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.95rem;
    padding: 10px 14px;
}
.stTextInput input:focus {
    border-color: #4285f4;
    box-shadow: 0 0 0 2px rgba(66,133,244,0.15);
}

/* ── Buttons ── */
.stButton > button {
    background: linear-gradient(135deg, #1a73e8 0%, #0d47a1 100%);
    color: #fff;
    border: none;
    border-radius: 6px;
    font-family: 'DM Sans', sans-serif;
    font-weight: 600;
    padding: 0.5rem 1.2rem;
    width: 100%;
    transition: opacity 0.15s;
}
.stButton > button:hover { opacity: 0.88; }

/* ── Checkboxes ── */
.stCheckbox label { color: #9ab0d0 !important; font-size: 0.88rem; }

/* ── Date inputs ── */
.stDateInput input {
    background: #0a1628;
    border-color: #1e3a5f;
    color: #e8f0fe;
    border-radius: 6px;
}

/* ── Expander ── */
.stExpander {
    border: 1px solid #132035 !important;
    border-radius: 6px !important;
    background: #07111f !important;
}
.stExpander summary {
    color: #7a9cc4 !important;
    font-size: 0.83rem;
}

/* ── Metrics ── */
div[data-testid="stMetric"] {
    background: #0a1628;
    border: 1px solid #132035;
    border-radius: 8px;
    padding: 14px 18px;
}
div[data-testid="stMetricValue"] {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.6rem !important;
    color: #e8f0fe !important;
}
div[data-testid="stMetricLabel"] {
    color: #5f7a9a !important;
    font-size: 0.78rem !important;
    text-transform: uppercase;
    letter-spacing: 0.8px;
}

/* ── Sidebar ── */
[data-testid="stSidebar"] {
    background: #07111f;
    border-right: 1px solid #132035;
}

/* ── Divider ── */
hr { border-color: #132035; }

/* ── Cards ── */
.card {
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 10px;
}
.card-out {
    border: 1px solid #1e3a5f;
    background: #0a1628;
}
.card-in {
    border: 1px solid #3d1a1a;
    background: #160808;
}
.card-header-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 10px;
}
.card-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.05rem;
    font-weight: 600;
    color: #e8f0fe;
}
.card-title-in { color: #fce8e8; }
.card-tag {
    margin-left: 10px;
    color: #5a8ad4;
    font-size: 0.8rem;
}
.card-tag-in { color: #c97070; }
.card-status { font-size: 0.88rem; color: #c4cfea; }
.status-code { color: #3d5a7a; font-size: 0.78rem; margin-left: 6px; }
.card-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 16px 28px;
    font-size: 0.8rem;
    color: #6a84a0;
    margin-bottom: 6px;
}
.card-meta b { color: #5a8ad4; font-weight: 500; margin-right: 4px; }
.card-addr { font-size: 0.8rem; color: #6a84a0; }
.card-addr b { color: #5a8ad4; font-weight: 500; margin-right: 4px; }
.card-row { display: flex; flex-wrap: wrap; }

/* ── Linked returns badge ── */
.linked-badge {
    border-left: 3px solid #d45a5a;
    padding: 4px 10px;
    margin: -2px 0 10px 0;
    color: #d45a5a;
    font-weight: 600;
    font-size: 0.85rem;
}

/* ── Store toggle buttons ── */
.store-active > button {
    background: linear-gradient(135deg, #1d5aaa 0%, #0d3a7a 100%) !important;
    border: 1px solid #4285f4 !important;
}

/* ── Hint text ── */
.hint {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.72rem;
    color: #3d5a7a;
    margin-top: 4px;
}

/* ── Tab headers ── */
.tab-label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.75rem;
    color: #5a8ad4;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    margin-bottom: 14px;
}

/* ── Warning / error pills ── */
.warn-pill {
    background: #2a1a00;
    border: 1px solid #6b3d00;
    border-radius: 5px;
    padding: 8px 14px;
    font-size: 0.8rem;
    color: #ffaa44;
    margin-bottom: 10px;
}
.err-pill {
    background: #200a0a;
    border: 1px solid #6b1010;
    border-radius: 5px;
    padding: 8px 14px;
    font-size: 0.8rem;
    color: #ff6b6b;
    margin-bottom: 6px;
}
</style>
""", unsafe_allow_html=True)


# ── Header ───────────────────────────────────────────────────────────────────

st.markdown("""
<div style="padding:24px 0 16px 0; border-bottom:1px solid #0f2040; margin-bottom:24px;">
    <div style="display:flex; align-items:baseline; gap:10px;">
        <span style="font-family:'IBM Plex Mono',monospace; font-size:1.5rem;
                     font-weight:700; color:#e8f0fe;">📦 Parcelninja</span>
        <span style="font-family:'IBM Plex Mono',monospace; font-size:1.5rem;
                     color:#2d5a9e;">/ Search</span>
    </div>
    <div style="color:#3d5a7a; font-size:0.82rem; margin-top:4px;">
        Multi-store shipment &amp; returns lookup · Bounty Brands + Levi's Hub
    </div>
</div>
""", unsafe_allow_html=True)


# ── Load stores (fail-fast on missing secrets) ────────────────────────────────

try:
    STORES = load_stores()
except RuntimeError as e:
    st.error(f"**Configuration error:** {e}")
    st.stop()


# ── Session state ─────────────────────────────────────────────────────────────

if "selected_stores" not in st.session_state:
    st.session_state.selected_stores = set(STORES.keys())


# ── Controls layout ───────────────────────────────────────────────────────────

ctrl_col, opts_col = st.columns([3, 1])

with ctrl_col:
    st.markdown("##### Stores")

    store_cols = st.columns(len(STORES))
    for i, sn in enumerate(STORES.keys()):
        with store_cols[i]:
            active  = sn in st.session_state.selected_stores
            label   = f"✓ {sn}" if active else sn
            css_cls = "store-active" if active else ""

            # Wrap in a div so we can apply the active class
            st.markdown(f'<div class="{css_cls}">', unsafe_allow_html=True)
            if st.button(label, key=f"btn_{sn}", use_container_width=True):
                if active:
                    st.session_state.selected_stores.discard(sn)
                else:
                    st.session_state.selected_stores.add(sn)
                st.rerun()           # ← fixed: was st.experimental_rerun()
            st.markdown("</div>", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    d1, d2 = st.columns(2)
    with d1:
        start_date = st.date_input("From", value=datetime.now() - timedelta(days=90))
    with d2:
        end_date = st.date_input("To", value=datetime.now())

    if start_date > end_date:
        st.error("'From' date must be before 'To' date.")
        st.stop()

    st.markdown("<br>", unsafe_allow_html=True)

    sq, sb = st.columns([4, 1])
    with sq:
        query = st.text_input(
            "query",
            placeholder="D20388  ·  SHP-1234  ·  PNJ-ID  ·  waybill  ·  client ref",
            label_visibility="collapsed",
        )
        st.markdown(
            '<div class="hint">Shopify order ref · PNJ ID · Client ref · Waybill · Supplier ref · Customer name</div>',
            unsafe_allow_html=True,
        )
    with sb:
        search_clicked = st.button("🔍 Search", use_container_width=True)


with opts_col:
    st.markdown("##### Search In")
    do_outbounds    = st.checkbox("Outbounds (Shipments)",       value=True)
    do_inbounds     = st.checkbox("Inbounds (Returns / Stock)",  value=True)
    do_link_returns = st.checkbox(
        "Auto-link Returns",
        value=True,
        help="Pre-fetches linked return inbounds for each matched shipment.",
    )

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("""
    <div style="border:1px solid #1e3a5f; border-radius:7px; padding:12px;
                background:#0a1628; font-size:0.77rem; color:#3d6494; line-height:1.7;">
        <strong style="color:#5a8ad4; display:block; margin-bottom:4px;">Matches against</strong>
        Channel ID (Shopify ref)<br>
        Client Ref (ERP ref)<br>
        PNJ ID (warehouse ID)<br>
        Waybill <em style="color:#2a4a6a;">(outbounds)</em><br>
        Supplier Ref <em style="color:#2a4a6a;">(inbounds)</em><br>
        Customer Name<br>
        <br>
        <strong style="color:#5a8ad4;">Cap:</strong> 2,000 records / store / type<br>
        <strong style="color:#5a8ad4;">Default window:</strong> 90 days
    </div>
    """, unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)


# ── Search execution ──────────────────────────────────────────────────────────

if search_clicked:
    if not query.strip():
        st.warning("Enter a search term.")
        st.stop()

    selected = list(st.session_state.selected_stores)
    if not selected:
        st.warning("Select at least one store.")
        st.stop()

    if not do_outbounds and not do_inbounds:
        st.warning("Enable at least one search type (Outbounds / Inbounds).")
        st.stop()

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt   = datetime.combine(end_date,   datetime.max.time())

    with st.spinner(f"Searching {len(selected)} store(s) for **{query}** …"):
        outbounds, inbounds, errors, capped = run_search(
            query               = query,
            selected_stores     = selected,
            search_outbounds_flag = do_outbounds,
            search_inbounds_flag  = do_inbounds,
            link_returns        = do_link_returns,
            start_date          = start_dt,
            end_date            = end_dt,
        )

    # ── Warnings & errors (operational visibility) ────────────────────────────
    if capped:
        st.markdown(
            "<div class='warn-pill'>⚠️ One or more stores hit the 2,000-record cap. "
            "Narrow the date range to ensure all matching records are returned.</div>",
            unsafe_allow_html=True,
        )

    if errors:
        with st.expander(f"⚠️ {len(errors)} API error(s) encountered — click to inspect"):
            for err in errors:
                st.markdown(f"<div class='err-pill'>{err}</div>", unsafe_allow_html=True)

    # ── Results ───────────────────────────────────────────────────────────────
    total = len(outbounds) + len(inbounds)

    if total == 0:
        st.info(
            f"No results for **{query}** across {len(selected)} store(s) "
            f"({start_date} → {end_date}). "
            "Try widening the date range or checking the reference format."
        )
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Results",       total)
        m2.metric("Shipments (Out)",     len(outbounds))
        m3.metric("Inbounds / Returns",  len(inbounds))
        m4.metric("Stores Searched",     len(selected))

        st.markdown("---")

        if outbounds and inbounds:
            tab_out, tab_in = st.tabs([
                f"📦  Outbounds ({len(outbounds)})",
                f"↩  Inbounds / Returns ({len(inbounds)})",
            ])
        elif outbounds:
            tab_out, tab_in = st.container(), None
        else:
            tab_out, tab_in = None, st.container()

        if outbounds and tab_out:
            with tab_out:
                st.markdown(
                    f"<div class='tab-label'>Shipments matching · {query}</div>",
                    unsafe_allow_html=True,
                )
                for ob in outbounds:
                    render_outbound_card(ob, show_linked_returns=do_link_returns)

        if inbounds and tab_in:
            with tab_in:
                st.markdown(
                    f"<div class='tab-label'>Inbounds / Returns matching · {query}</div>",
                    unsafe_allow_html=True,
                )
                for ib in inbounds:
                    render_inbound_card(ib)

else:
    # Landing state
    st.markdown("""
    <div style="text-align:center; padding:70px 0; color:#1a3a6a;">
        <div style="font-size:2.8rem;">📦</div>
        <div style="font-family:'IBM Plex Mono',monospace; font-size:0.95rem;
                    margin-top:14px; color:#2d4a6a;">
            Enter a reference above to search across all stores
        </div>
        <div style="font-size:0.78rem; margin-top:8px; color:#1a2e4a;">
            Channel ID (Shopify) · Client Ref · PNJ ID · Supplier Ref · Waybill · Customer Name
        </div>
    </div>
    """, unsafe_allow_html=True)
