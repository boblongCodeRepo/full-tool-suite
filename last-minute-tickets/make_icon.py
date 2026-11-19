"""Draws the Last-Minute Tickets icon (icon.ico for shortcuts, static/icon.png for the window)."""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
S = 256
BG, ORANGE, DARK, WHITE = (13, 17, 23, 255), (240, 136, 62, 255), (13, 17, 23, 255), (230, 237, 243, 255)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=46, fill=BG, outline=ORANGE, width=10)
# ticket with notches
d.rounded_rectangle([36, 78, 220, 178], radius=14, fill=ORANGE)
for cy in (128,):
    d.ellipse([22, cy - 16, 50, cy + 16], fill=BG)
    d.ellipse([206, cy - 16, 234, cy + 16], fill=BG)
for y in range(88, 172, 14):
    d.line([(160, y), (160, y + 7)], fill=DARK, width=4)
d.text((66, 96), "$", fill=DARK, font_size=64)
# clock (last minute)
d.ellipse([150, 150, 230, 230], fill=WHITE, outline=DARK, width=6)
d.line([(190, 190), (190, 164)], fill=DARK, width=7)
d.line([(190, 190), (210, 198)], fill=DARK, width=7)
img.save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
(HERE / "static").mkdir(exist_ok=True)
img.save(HERE / "static" / "icon.png")
print("wrote icons")
