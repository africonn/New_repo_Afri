"""
tools/sap.py  (UPDATED — replaces stub)
========================================
SAP integration via OAuth2 tokens managed by sap_auth_handler.py.

Once the DC admin has clicked Authorize (one-time setup), Claude can:
  - Submit invoices directly to SAP
  - Post advance shipping notices (ASNs)
  - Query PO status
  - Submit remittance bundles

No human involvement needed after initial authorization.
Tokens auto-refresh transparently.
"""

import json
import os
import sys
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import Context

# Import token getter from app.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from app import get_valid_token

import httpx

SAP_API_BASE = os.getenv("SAP_API_BASE", "https://openapi.ariba.com")


class PostInvoiceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    po_number:      str   = Field(..., description="Purchase order number from the DC")
    invoice_number: str   = Field(..., description="Your invoice reference number")
    invoice_date:   str   = Field(..., description="Invoice date YYYY-MM-DD")
    line_items:     list  = Field(..., description="List of {sku, qty, unit_price} dicts")
    total_zar:      float = Field(..., gt=0, description="Total invoice value in ZAR")
    currency:       str   = Field(default="ZAR")


class PostASNInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    po_number:     str  = Field(..., description="Purchase order number")
    asn_number:    str  = Field(..., description="Your delivery/ASN reference")
    delivery_date: str  = Field(..., description="Actual delivery date YYYY-MM-DD")
    store_code:    str  = Field(..., description="SPAR store code from PO")
    line_items:    list = Field(..., description="List of {sku, qty_delivered} dicts")


class GetPOStatusInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    po_number: str = Field(..., description="PO number to check status for")


async def post_invoice(params: PostInvoiceInput, ctx: Context) -> str:
    """Submit a supplier invoice directly to SAP via Ariba Network API."""
    await ctx.info(f"Submitting invoice {params.invoice_number} to SAP...")

    try:
        token = get_valid_token()
    except Exception as e:
        return json.dumps({"error": str(e), "action": "DC must complete SAP authorization first"})

    payload = {
        "supplierInvoiceID":     params.invoice_number,
        "invoicingParty":        os.getenv("SUPPLIER_ID", "AFRICONN"),
        "documentDate":          params.invoice_date,
        "purchaseOrder":         params.po_number,
        "invoiceCurrency":       params.currency,
        "grossAmountInCurrency": str(params.total_zar),
        "supplierInvoiceItemList": [
            {
                "supplierInvoiceItem": str(i + 1).zfill(6),
                "purchaseOrderItem":   str(i + 1).zfill(6),
                "plant":               params.line_items[i].get("store_code", ""),
                "invoiceQuantity":     str(item["qty"]),
                "unitPrice":           str(item["unit_price"]),
                "netAmount":           str(round(item["qty"] * item["unit_price"], 2)),
            }
            for i, item in enumerate(params.line_items)
        ],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{SAP_API_BASE}/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
        )

    if response.status_code in (200, 201):
        result = response.json()
        return json.dumps({
            "status":         "submitted",
            "invoice_number": params.invoice_number,
            "sap_document":   result.get("d", {}).get("SupplierInvoice", ""),
            "message":        f"Invoice {params.invoice_number} successfully posted to SAP",
        }, indent=2)
    else:
        return json.dumps({"status": "error", "http": response.status_code, "detail": response.text[:500]}, indent=2)


async def post_asn(params: PostASNInput, ctx: Context) -> str:
    """Post an Advance Shipping Notice to SAP — confirms delivery to store."""
    await ctx.info(f"Posting ASN {params.asn_number} for PO {params.po_number}...")

    try:
        token = get_valid_token()
    except Exception as e:
        return json.dumps({"error": str(e)})

    payload = {
        "DeliveryDocument":      params.asn_number,
        "PurchaseOrder":         params.po_number,
        "PlannedGoodsIssueDate": params.delivery_date,
        "ShipToParty":           params.store_code,
        "DeliveryDocumentItem": [
            {
                "DeliveryDocumentItem":   str(i + 1).zfill(6),
                "Material":               item.get("sku", ""),
                "ActualDeliveryQuantity": str(item["qty_delivered"]),
            }
            for i, item in enumerate(params.line_items)
        ],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{SAP_API_BASE}/sap/opu/odata/sap/API_OUTBOUND_DELIVERY_SRV/A_OutbDeliveryHeader",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
        )

    if response.status_code in (200, 201):
        return json.dumps({
            "status":     "asn_posted",
            "asn_number": params.asn_number,
            "po_number":  params.po_number,
            "store":      params.store_code,
            "message":    "ASN confirmed — store notified of delivery",
        }, indent=2)
    else:
        return json.dumps({"status": "error", "http": response.status_code, "detail": response.text[:500]}, indent=2)


async def get_po_status(params: GetPOStatusInput, ctx: Context) -> str:
    """Query SAP for current status of a purchase order."""
    await ctx.info(f"Checking PO status: {params.po_number}")

    try:
        token = get_valid_token()
    except Exception as e:
        return json.dumps({"error": str(e)})

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{SAP_API_BASE}/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder('{params.po_number}')",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    if response.status_code == 200:
        data = response.json().get("d", {})
        return json.dumps({
            "po_number":     params.po_number,
            "status":        data.get("ProcessingStatus", "unknown"),
            "vendor":        data.get("Supplier", ""),
            "total_amount":  data.get("TotalNetAmount", ""),
            "currency":      data.get("DocumentCurrency", "ZAR"),
            "delivery_date": data.get("DeliveryDate", ""),
        }, indent=2)
    else:
        return json.dumps({"status": "error", "http": response.status_code, "detail": response.text[:300]}, indent=2)
