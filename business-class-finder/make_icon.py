"""Draws the Business Class Finder icon (icon.ico for the shortcut, static/icon.png for the window)."""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
S = 256
NAVY, GOLD, AMBER = (11, 18, 32, 255), (251, 191, 36, 255), (245, 158, 11, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=46, fill=NAVY, outline=AMBER, width=10)

plane = Image.new("RGBA", (S, S), (0, 0, 0, 0))
p = ImageDraw.Draw(plane)
p.rounded_rectangle([114, 34, 142, 222], radius=14, fill=GOLD)                        # fuselage
p.polygon([(128, 96), (222, 146), (222, 166), (128, 138), (34, 166), (34, 146)], fill=GOLD)  # wings
p.polygon([(128, 184), (172, 212), (172, 224), (128, 206), (84, 224), (84, 212)], fill=GOLD)  # tail
plane = plane.rotate(-40, resample=Image.BICUBIC, center=(128, 128))
img.alpha_composite(plane)

img.save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
(HERE / "static").mkdir(exist_ok=True)
img.save(HERE / "static" / "icon.png")
print("wrote icons")
