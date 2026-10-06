"""Builds the GOLDEN extraction fixtures: what a perfect reader would return
for each vendor file. Used ONLY by tests to check matching + normalization
math against the answer key. The app never loads these."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.rfq import load_rfq
from core.schema import *

src = json.load(open(sys.argv[1]))
Q, card = src["quotes"], src["card"]
R = {l.code: l for l in load_rfq()}
E = lambda q, loc=None: Evidence(quote=q, location=loc)
out = {}

# Kraftline: own codes, per 1000, delivered, ex-GST, 27 lines + 2 alternates
items = []
for code, d in Q["Kraftline"].items():
    base = code.replace("-ALT", ""); l = R[base]; alt = "ALT" in code
    items.append(QuotedItem(vendor_ref=f"KP-{l.ply}xxx{'A' if alt else ''}", description=f"{l.box_type} {l.ply} Ply {l.dims_label} mm",
        product_type={"RSC":"box","Tray":"tray","Die-cut":"die_cut","Partition":"partition","Pad":"pad"}[l.box_type],
        size_as_written=f"{l.dims_label} mm", size_unit="mm", ply=l.ply, paper_spec="18BF" if alt else l.paper_spec,
        print_colours=l.print_colours, price_text=f"{d['raw']:,}", price_value=d["raw"], currency="INR",
        price_unit=PriceUnit.per_1000, is_alternate_spec=alt, alternate_note="value engineered" if alt else None,
        evidence=E(f"{d['raw']:,}", "Quotation sheet")))
out["Kraftline"] = VendorResponse(vendor_name="Kraftline", source_files=["1_Kraftline_Offer.xlsx"], gst_treatment="exclusive",
    freight_treatment="delivered", payment_days=45, validity_days=90, items=items)

# Shree Balaji: our codes, GST-incl, 3% conditional discount
items = []
for code, d in Q["Shree Balaji"].items():
    l = R[code]
    items.append(QuotedItem(vendor_ref=code, description=l.description, size_as_written=l.dims_label, size_unit="mm", ply=l.ply,
        price_text=f"{d['raw']:.2f}*", price_value=d["raw"], currency="INR",
        price_unit=PriceUnit.per_kg if d["unit"] == "per kg" else PriceUnit.per_piece, evidence=E(f"{d['raw']:.2f}*", "p.1")))
out["Shree Balaji"] = VendorResponse(vendor_name="Shree Balaji", source_files=["2_ShreeBalaji_Quotation.pdf"], gst_treatment="inclusive",
    gst_rate_percent=18, freight_treatment="delivered", payment_days=60, validity_days=60,
    discounts=[Discount(description="special discount", percent=3, condition="PO value above ₹10 lakh per PO", evidence=E("Additional 3% special discount"))],
    items=items)

# Pacific: our codes, USD per piece
items = [QuotedItem(vendor_ref=c, description=R[c].description, price_text=f"USD {d['raw']:.3f}", price_value=d["raw"], currency="USD",
         price_unit=PriceUnit.per_piece, evidence=E(f"USD {d['raw']:.3f}")) for c, d in Q["Pacific"].items()]
out["Pacific"] = VendorResponse(vendor_name="Pacific", source_files=["3_Pacific_Proposal.docx"], gst_treatment="exclusive",
    freight_treatment="delivered", payment_days=30, validity_days=15, items=items)

# Gupta: every rate-card cell, inch sizes, per 100, plain, ex-works
items = []
for row in card:
    for ply in ("3", "5"):
        if ply in row:
            items.append(QuotedItem(description=f"{row['size']} {ply} ply", product_type="box", size_as_written=row["size"], size_unit="inch", ply=int(ply),
                price_text=f"{row[ply]:,}", price_value=row[ply], currency="INR", price_unit=PriceUnit.per_100, plain_box_rate=True,
                handwritten=f"old{ply}" in row, evidence=E(f"{row['size']} | {row[ply]:,}", "rate card")))
items.append(QuotedItem(description="Pad 16x12 - 3ply", product_type="pad", size_as_written="16x12", size_unit="inch", ply=3, price_text="185/100",
    price_value=Q["Gupta"]["PD-101"]["raw"], currency="INR", price_unit=PriceUnit.per_100, handwritten=True, evidence=E("Pad 16x12 - 3ply 185/100")))
out["Gupta"] = VendorResponse(vendor_name="Gupta", source_files=["4_Gupta_RateCard_photo.jpg"], gst_treatment="exclusive",
    freight_treatment="ex_works", adders=[Adder(description="printing Rs.50 per 100 per colour", amount=50, unit="per_100_per_colour", evidence=E("Printing Rs.50 per 100 per colour extra"))],
    items=items)

# Om Sai: two category rates + "rest same as last year"
items = [
  QuotedItem(description="5-ply", ply=5, price_text="₹42/kg", price_value=42, currency="INR", price_unit=PriceUnit.per_kg, scope="category", scope_description="the 5-ply", evidence=E("₹42/kg for the 5-ply")),
  QuotedItem(description="3-ply", ply=3, price_text="38", price_value=38, currency="INR", price_unit=PriceUnit.per_kg, scope="category", scope_description="the 3-ply", evidence=E("38 for the 3-ply")),
  QuotedItem(description="rest", price_text="rest same as last year", currency="INR", price_unit=PriceUnit.same_as_last_year, scope="all_remaining", scope_description="rest same as last year", evidence=E("rest same as last year")),
]
out["Om Sai"] = VendorResponse(vendor_name="Om Sai", source_files=["5_OmSai_reply.eml"], gst_treatment="not_stated", freight_treatment="extra",
    refers_to_prior=["rest same as last year", "questionnaire same as last yr"], items=items)

for v, r in out.items():
    Path(__file__).parent.joinpath("fixtures", f"golden_{v.replace(' ', '_')}.json").write_text(r.model_dump_json(indent=1))
print("wrote", list(out))
