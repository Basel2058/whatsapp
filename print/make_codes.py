from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, black
from reportlab.graphics.barcode.code128 import Code128
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF

URL = "https://basel2058.github.io/whatsapp/"
OUT = "/home/user/whatsapp/print/open-chat-barcode.pdf"
W, H = A4
green, deep, muted = HexColor("#25d366"), HexColor("#128c7e"), HexColor("#6b7772")

c = canvas.Canvas(OUT, pagesize=A4)
c.setTitle("Open Chat barcode")

# Header
c.setFillColor(deep); c.roundRect(W/2 - 9*mm, H - 40*mm, 18*mm, 18*mm, 5*mm, fill=1, stroke=0)
c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 16); c.drawCentredString(W/2, H - 33.5*mm, "OC")
c.setFillColor(black); c.setFont("Helvetica-Bold", 26); c.drawCentredString(W/2, H - 55*mm, "Open Chat")
c.setFillColor(muted); c.setFont("Helvetica", 11)
c.drawCentredString(W/2, H - 63*mm, "Message any number on WhatsApp without saving it to your contacts.")

# Code 128 barcode for laser scanners (black on white, wide quiet zones)
bc = Code128(URL, barWidth=0.33*mm, barHeight=28*mm, humanReadable=False, quiet=True,
             lquiet=8*mm, rquiet=8*mm)
x = (W - bc.width) / 2
y = H - 110*mm
c.setFillColor(black); c.setStrokeColor(black)
bc.drawOn(c, x, y)
c.setFillColor(black); c.setFont("Courier", 11); c.drawCentredString(W/2, y - 7*mm, URL)
c.setFillColor(muted); c.setFont("Helvetica", 10)
c.drawCentredString(W/2, y - 14*mm, "Laser / USB barcode scanner: click the browser's address bar, then scan.")

# Divider
c.setStrokeColor(HexColor("#e3e8e6")); c.setLineWidth(0.6); c.line(40*mm, y - 26*mm, W - 40*mm, y - 26*mm)

# QR code for phones
size = 55*mm
qr = QrCodeWidget(URL, barLevel="M")
b = qr.getBounds(); bw, bh = b[2]-b[0], b[3]-b[1]
d = Drawing(size, size, transform=[size/bw, 0, 0, size/bh, 0, 0]); d.add(qr)
qy = y - 30*mm - size - 4*mm
c.setFillColor(black)
renderPDF.draw(d, c, (W - size)/2, qy)
c.setFillColor(muted); c.setFont("Helvetica", 10)
c.drawCentredString(W/2, qy - 6*mm, "Phone: point your camera at the QR code.")

# Footer
c.setFillColor(muted); c.setFont("Helvetica", 8.5)
c.drawCentredString(W/2, 15*mm, "Built by T.B.Technologies Inc.")
c.showPage(); c.save()
print("barcode width mm:", round(bc.width/mm, 1))
