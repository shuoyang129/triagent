from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 640
out_dir = Path(__file__).resolve().parents[1] / "assets"
out_dir.mkdir(exist_ok=True)
out = out_dir / "social-preview.png"
out_jpg = Path(
    r"C:\Users\yangs\Documents\Codex\2026-08-12\new-chat\outputs\triagent-social-preview.jpg"
)
out_jpg.parent.mkdir(parents=True, exist_ok=True)

img = Image.new("RGB", (W, H), "#10161C")
draw = ImageDraw.Draw(img)

for i in range(0, W + H, 28):
    c = 18 + (i // 80) % 6
    draw.line([(i, 0), (i - H, H)], fill=(c, c + 4, c + 8), width=1)

draw.rectangle([0, 0, 14, H], fill="#1FA6A0")
draw.rounded_rectangle([72, 78, 1208, 562], radius=28, fill="#161E27", outline="#263242", width=2)

candidates = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
]
body_candidates = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


def load(paths, size):
    for path in paths:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


font_brand = load(candidates, 92)
font_tag = load(body_candidates, 34)
font_cn = load(body_candidates, 30)
font_chip = load(body_candidates, 26)
font_small = load(body_candidates, 22)

draw.text((118, 130), "TriAgent", font=font_brand, fill="#F4F7FA")
draw.text((118, 250), "Approval-gated multi-agent software delivery", font=font_tag, fill="#9BB0C3")
draw.text((118, 300), "实现 · 验证 · 审查分工，关键决策留给人类", font=font_cn, fill="#D7E2EC")

chips = [
    ("Implement", "#1FA6A0"),
    ("Verify", "#3D8BDB"),
    ("Review", "#C9852E"),
    ("Approve", "#E8EDF2"),
]
x = 118
y = 390
for i, (label, color) in enumerate(chips):
    tw = draw.textlength(label, font=font_chip)
    pad_x, pad_y = 22, 14
    box = [x, y, x + tw + pad_x * 2, y + 52]
    if label == "Approve":
        draw.rounded_rectangle(box, radius=16, fill="#243041", outline="#E8EDF2", width=2)
        text_fill = "#E8EDF2"
    else:
        draw.rounded_rectangle(box, radius=16, fill=color)
        text_fill = "#0B1218"
    draw.text((x + pad_x, y + pad_y), label, font=font_chip, fill=text_fill)
    x = box[2] + 18
    if i < len(chips) - 1:
        draw.polygon([(x, y + 18), (x + 14, y + 26), (x, y + 34)], fill="#6B7C8F")
        x += 28

draw.text((118, 490), "github.com/shuoyang129/triagent", font=font_small, fill="#7F93A8")

img.save(out, format="PNG", optimize=True)
img.convert("RGB").save(out_jpg, format="JPEG", quality=90, optimize=True)
print(out, out.stat().st_size)
print(out_jpg, out_jpg.stat().st_size)
