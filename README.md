# Parcelninja Multi-Store Search

A Streamlit app to search shipments and returns across all Bounty Brands + Levi's warehouse stores.

## Setup

```bash
pip install -r requirements.txt
streamlit run pnj_search.py
```

## Features

- **Cross-store search** across Diesel, Hurley, Jeep Apparel, Superdry, Reebok, Levi's simultaneously
- **Search by any reference**: Shopify Channel ID (e.g. `D20388`), Client Ref, PNJ ID, Supplier Ref
- **Outbounds**: Full shipment detail — status, courier, tracking, items with inventory name lookup
- **Inbounds**: Returns and stock arrivals with variance detection and event timeline
- **SHP → RET linking**: For each outbound match, automatically finds linked return inbounds by matching clientId/channelId
- **Configurable date range**: Default 180 days, adjustable in sidebar
- **SKU name resolution**: Cached inventory lookups for descriptive product names

## Search Behaviour

The API doesn't expose a direct channelId filter, so the app paginates through results
(up to 1,000 records per store per search type) and matches client-side. Narrow the
date range to speed up searches on specific date windows.

### SHP → RET Link Logic

When "Auto-link Returns" is enabled, after finding an outbound the app searches inbounds
for records where `clientId` or `supplierReference` contains the outbound's `clientId` or `channelId`.
This handles the standard Parcelninja return flow where the inbound carries the original order reference.

## Deployment (Streamlit Cloud)

1. Push to a private GitHub repo
2. Connect via share.streamlit.io
3. Set secrets (optional — credentials are currently hardcoded; move to `st.secrets` for production)

```toml
# .streamlit/secrets.toml
[diesel]
username = "..."
password = "..."
store_id = "..."
```
