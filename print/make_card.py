"""Builds a 10 x 10 cm desk card (on an A4 sheet with cut marks) for the Open Chat site."""
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, black, white
from reportlab.graphics.barcode.code128 import Code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF

SCAN_TEXT = "basel2058.github.io/whatsapp"          # what the laser scanner types
QR_URL = "https://basel2058.github.io/whatsapp/"   # what phones open
OUT = "open-chat-card.pdf"
CARD = 100 * mm
deep, muted, line = HexColor("#128c7e"), HexColor("#6b7772"), HexColor("#c9d2ce")

W, H = A4
cx0, cy0 = (W - CARD) / 2, (H - CARD) / 2   # card's bottom-left corner on the sheet
c = canvas.Canvas(OUT, pagesize=A4)
c.setTitle("Open Chat desk card")

# Cut marks at the four corners, outside the card
c.setStrokeColor(muted); c.setLineWidth(0.4)
for x in (cx0, cx0 + CARD):
    for y in (cy0, cy0 + CARD):
        dx = -1 if x == cx0 else 1
        dy = -1 if y == cy0 else 1
        c.line(x + dx * 2*mm, y, x + dx * 8*mm, y)
        c.line(x, y + dy * 2*mm, x, y + dy * 8*mm)
c.setFont("Helvetica", 8); c.setFillColor(muted)
c.drawCentredString(W/2, cy0 - 12*mm, "Print at 100% (Actual size). Cut along the corner marks: the card is 10 x 10 cm.")

def cx(): return cx0 + CARD / 2
top = cy0 + CARD

# Title
c.setFillColor(black); c.setFont("Helvetica-Bold", 15)
c.drawCentredString(cx(), top - 11*mm, "Open Chat")
c.setFillColor(muted); c.setFont("Helvetica", 7.5)
c.drawCentredString(cx(), top - 15.5*mm, "Message any number on WhatsApp without saving it.")

# Code 128 barcode: 0.254 mm bars (10 mil) = whole printer dots at 300 and 600 dpi
bc = Code128(SCAN_TEXT, barWidth=0.254*mm, barHeight=19*mm, humanReadable=False,
             quiet=True, lquiet=2.6*mm, rquiet=2.6*mm)
assert bc.width <= CARD - 4*mm, f"barcode too wide: {bc.width/mm:.1f} mm"
by = top - 40*mm
c.setFillColor(black); c.setStrokeColor(black)
bc.drawOn(c, cx() - bc.width / 2, by)
c.setFont("Courier", 7.5); c.drawCentredString(cx(), by - 4*mm, SCAN_TEXT)

# QR code for phones
size = 38*mm
qr = QrCodeWidget(QR_URL, barLevel="M")
b = qr.getBounds(); bw, bh = b[2] - b[0], b[3] - b[1]
d = Drawing(size, size, transform=[size/bw, 0, 0, size/bh, 0, 0]); d.add(qr)
qy = by - 8*mm - size
c.setFillColor(black)
renderPDF.draw(d, c, cx() - size / 2, qy)

# Footer
c.setFillColor(muted); c.setFont("Helvetica", 6.5)
c.drawCentredString(cx(), cy0 + 5*mm, "Built by T.B.Technologies Inc.")

c.showPage(); c.save()
print(f"barcode width incl. quiet zones: {bc.width/mm:.1f} mm; QR bottom at {(qy-cy0)/mm:.1f} mm from card edge")
