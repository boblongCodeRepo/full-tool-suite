"""Draws the Used Car Deal Finder icon (icon.ico for shortcuts, static/icon.png for the window)."""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
S = 256
BG, GREEN, DARK, WHITE, GLASS = (13, 17, 23, 255), (63, 185, 80, 255), (13, 17, 23, 255), (230, 237, 243, 255), (88, 166, 255, 255)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=46, fill=BG, outline=GREEN, width=10)
# car body (side view)
d.rounded_rectangle([34, 120, 222, 176], radius=22, fill=GREEN)
d.polygon([(72, 122), (98, 80), (164, 80), (196, 122)], fill=GREEN)
d.polygon([(86, 118), (104, 90), (126, 90), (126, 118)], fill=GLASS)
d.polygon([(134, 118), (134, 90), (158, 90), (180, 118)], fill=GLASS)
for cx in (78, 178):
    d.ellipse([cx - 25, 152, cx + 25, 202], fill=DARK)
    d.ellipse([cx - 13, 164, cx + 13, 190], fill=WHITE)
# price tag
d.rounded_rectangle([150, 26, 230, 66], radius=10, fill=WHITE)
d.text((162, 30), "$", fill=DARK, font_size=32)
d.line([(192, 34), (214, 58)], fill=GREEN, width=7)
d.line([(214, 58), (220, 44)], fill=GREEN, width=7)
img.save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
(HERE / "static").mkdir(exist_ok=True)
img.save(HERE / "static" / "icon.png")
print("wrote icons")
