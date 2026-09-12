"""Convert PNG to raw RGB565 binary for ESP32 LVGL (little-endian)."""
import sys
import struct
from PIL import Image

def convert(src, dst, max_w=None):
    img = Image.open(src).convert("RGBA")
    if max_w and img.width > max_w:
        ratio = max_w / img.width
        img = img.resize((max_w, int(img.height * ratio)), Image.LANCZOS)

    bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
    bg.paste(img, (0, 0), img)
    rgb = bg.convert("RGB")

    w, h = rgb.size
    with open(dst, "wb") as f:
        for r, g, b in rgb.getdata():
            rgb565 = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            f.write(struct.pack("<H", rgb565))

    print(f"{dst}: {w}x{h}, {w*h*2} bytes")
    return w, h

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python convert_logo.py input.png output.bin [max_width]")
        sys.exit(1)
    max_w = int(sys.argv[3]) if len(sys.argv) > 3 else None
    convert(sys.argv[1], sys.argv[2], max_w)
