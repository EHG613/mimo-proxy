"""生成 App 图标 — 深蓝→青渐变背景 + 字母 M（上黑下白）"""
from PIL import Image, ImageDraw, ImageChops
import os

SIZE = 512
OUTPUT = os.path.join(os.path.dirname(__file__), "..", "src-tauri", "icons", "icon.png")

# ── 1. 纯色背景：上半白、下半黑 ──
bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
bg_draw = ImageDraw.Draw(bg)
bg_draw.rectangle([0, 0, SIZE, SIZE // 2], fill=(255, 255, 255, 255))
bg_draw.rectangle([0, SIZE // 2, SIZE, SIZE], fill=(0, 0, 0, 255))

# ── 2. 圆角遮罩 ──
radius = int(SIZE * 0.225)
corner_mask = Image.new("L", (SIZE, SIZE), 0)
mask_draw = ImageDraw.Draw(corner_mask)
mask_draw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=radius, fill=255)

# ── 3. 字母 M ──
cx, cy = SIZE // 2, SIZE // 2
sw = int(SIZE * 0.10)      # 稍微加粗
lw = int(SIZE * 0.23)
lh = int(SIZE * 0.28)

m_mask = Image.new("L", (SIZE, SIZE), 0)
m_mask_draw = ImageDraw.Draw(m_mask)
m_mask_draw.line([(cx - lw, cy - lh), (cx - lw, cy + lh)], fill=255, width=sw)
m_mask_draw.line([(cx + lw, cy - lh), (cx + lw, cy + lh)], fill=255, width=sw)
m_mask_draw.line([(cx - lw, cy - lh), (cx, cy)], fill=255, width=sw)
m_mask_draw.line([(cx + lw, cy - lh), (cx, cy)], fill=255, width=sw)
# 在中心加一个小圆，确保交汇处不镂空
m_mask_draw.ellipse([cx - sw//2, cy - sw//2, cx + sw//2, cy + sw//2], fill=255)

bottom_mask = Image.new("L", (SIZE, SIZE), 0)
bottom_draw = ImageDraw.Draw(bottom_mask)
bottom_draw.rectangle([0, cy + 1, SIZE, SIZE], fill=255)
m_bottom_mask = ImageChops.multiply(m_mask, bottom_mask)
m_top_mask = ImageChops.subtract(m_mask, m_bottom_mask)  # 上半区

# ── 4. 直接合成到背景上 ──
result = bg.copy()

# 白色发光背景（M 后面，让 M 在渐变背景上突出）
glow_mask = Image.new("L", (SIZE, SIZE), 0)
glow_mask_draw = ImageDraw.Draw(glow_mask)
for g in range(sw + 14, sw, -4):
    alpha = max(20, 70 - (g - sw) * 5)
    glow_mask_draw.line([(cx - lw, cy - lh), (cx - lw, cy + lh)], fill=alpha, width=g)
    glow_mask_draw.line([(cx + lw, cy - lh), (cx + lw, cy + lh)], fill=alpha, width=g)
    glow_mask_draw.line([(cx - lw, cy - lh), (cx, cy)], fill=alpha, width=g)
    glow_mask_draw.line([(cx + lw, cy - lh), (cx, cy)], fill=alpha, width=g)
result.paste((255, 255, 255, 255), (0, 0), mask=glow_mask)

# 黑色 M（上半）
result.paste((0, 0, 0, 255), (0, 0), mask=m_top_mask)
# 白色 M（下半）
result.paste((255, 255, 255, 255), (0, 0), mask=m_bottom_mask)

# ── 5. 圆角裁剪 ──
result.putalpha(corner_mask)

os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
result.save(OUTPUT, "PNG")

# 验证
verify = Image.open(OUTPUT)
print(f"Icon: {verify.size}, mode={verify.mode}")
p = verify.getpixel((256, 50))
print(f"Top (256,50): {p} {'✓' if p[0] > 200 else '✗ expected white bg'}")
p = verify.getpixel((256, 256))
print(f"Center (256,256): {p} {'✓' if p[0] < 30 else '✗ expected black M'}")
p = verify.getpixel((256, 450))
print(f"Bottom (256,450): {p} {'✓' if p[0] < 50 else '✗ expected dark'}")

lw = int(SIZE * 0.23)
lh = int(SIZE * 0.28)
p = verify.getpixel((cx - lw, cy - lh))
print(f"M top-left ({cx-lw},{cy-lh}): {p} {'✓' if p[0] < 30 else '✗ expected black'}")
p = verify.getpixel((cx - lw, cy + lh))
print(f"M bot-left ({cx-lw},{cy+lh}): {p} {'✓' if p[0] > 200 else '✗ expected white'}")