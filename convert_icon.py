#!/usr/bin/env python3
"""Create ICNS icon for macOS matching SDR-style colorful waveform."""

import subprocess
import os
from pathlib import Path
from PIL import Image, ImageDraw

def create_icon_image(size):
    """Create an icon image at the given size matching the SDR style."""
    # Create image with dark background
    img = Image.new('RGBA', (size, size), (255, 255, 255, 255))
    
    # Draw gradient background
    draw_gradient_background(img, size)
    
    draw = ImageDraw.Draw(img)
    
    # Scale factor
    scale = size / 1024
    
    # Draw colorful waveform spectrum background
    draw_colorful_waveform(draw, size, scale)
    
    # Draw FSB text at the top
    draw_fsb_text_top(draw, size, scale)
    
    return img

def draw_gradient_background(img, size):
    """Draw dark gradient background matching SDR style."""
    pixels = img.load()
    
    # Dark navy to slightly lighter navy gradient
    for y in range(size):
        # Gradient from top to bottom
        t = y / size
        # Dark navy gradient
        r = int(20 + 30 * t)
        g = int(30 + 40 * t)
        b = int(50 + 60 * t)
        
        for x in range(size):
            pixels[x, y] = (r, g, b, 255)

def draw_colorful_waveform(draw, size, scale):
    """Draw a colorful spectrum waveform like the SDR icon."""
    
    # Waveform dimensions - positioned in lower half
    wave_top = int(400 * scale)
    wave_bottom = int(900 * scale)
    wave_height = wave_bottom - wave_top
    wave_left = int(80 * scale)
    wave_right = int(944 * scale)
    wave_width = wave_right - wave_left
    
    # Create colorful bars (spectrum analyzer style)
    num_bars = 50
    bar_width = wave_width // num_bars
    
    # Color gradient: blue -> cyan -> green -> yellow -> orange -> red
    def get_bar_color(index, total):
        """Get color for bar based on position (spectrum gradient)."""
        t = index / max(total - 1, 1)
        
        if t < 0.2:  # Blue to Cyan
            r = int(0 + (100 - 0) * (t / 0.2))
            g = int(100 + (200 - 100) * (t / 0.2))
            b = int(220 + (255 - 220) * (t / 0.2))
        elif t < 0.35:  # Cyan to Green
            r = int(100 + (50 - 100) * ((t - 0.2) / 0.15))
            g = int(200 + (220 - 200) * ((t - 0.2) / 0.15))
            b = int(255 + (50 - 255) * ((t - 0.2) / 0.15))
        elif t < 0.5:  # Green to Yellow
            r = int(50 + (255 - 50) * ((t - 0.35) / 0.15))
            g = int(220 + (255 - 220) * ((t - 0.35) / 0.15))
            b = int(50 + (0 - 50) * ((t - 0.35) / 0.15))
        elif t < 0.7:  # Yellow to Orange
            r = int(255)
            g = int(255 + (140 - 255) * ((t - 0.5) / 0.2))
            b = int(0)
        else:  # Orange to Red
            r = int(255 + (200 - 255) * ((t - 0.7) / 0.3))
            g = int(140 + (50 - 140) * ((t - 0.7) / 0.3))
            b = int(0 + (20 - 0) * ((t - 0.7) / 0.3))
        
        return (r, g, b, 255)
    
    # Draw bars with varying heights (waveform pattern)
    import math
    for i in range(num_bars):
        # Calculate bar height based on sine wave pattern
        t = i / num_bars
        # Multiple sine waves for more interesting pattern
        amplitude = 0.5 + 0.5 * math.sin(t * math.pi * 3.5)
        amplitude *= 0.6 + 0.4 * math.cos(t * math.pi * 1.5)
        
        bar_height = int(wave_height * amplitude * 0.85)
        
        bar_left = wave_left + (i * bar_width) + int(3 * scale)
        bar_right = bar_left + int(bar_width - 6 * scale)
        bar_bottom = wave_bottom
        bar_top = wave_bottom - bar_height
        
        color = get_bar_color(i, num_bars)
        draw.rectangle([(bar_left, bar_top), (bar_right, bar_bottom)], fill=color, outline=None)

def draw_fsb_text_top(draw, size, scale):
    """Draw FSB text at the top in bold, upright font."""
    try:
        from PIL import ImageFont
        font_size = int(160 * scale)
        
        try:
            # Try to load bold Helvetica (upright, not italic)
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size=font_size, index=2)
        except:
            try:
                # Try Arial Bold
                font = ImageFont.truetype("/System/Library/Fonts/Arial.ttf", size=font_size)
            except:
                # Fallback
                font = ImageFont.load_default()
        
        text = "FSB"
        text_y = int(120 * scale)
        
        # Get text bounding box to center it horizontally
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        text_x = (size - text_width) // 2
        
        # Draw main white text (no shadow, just clean white)
        draw.text((text_x, text_y), text, fill=(255, 255, 255, 255), font=font)
        
    except Exception as e:
        print(f"Warning: Could not render text: {e}")

def create_macos_icon():
    """Create and save ICNS icon for macOS."""
    script_dir = Path(__file__).parent
    icns_file = script_dir / "FlexSpotBridge.icns"
    
    # Create temporary directory for iconset
    iconset_dir = script_dir / "FlexSpotBridge.iconset"
    os.makedirs(iconset_dir, exist_ok=True)
    
    # Icon sizes for macOS
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    
    print("Creating icon images with colorful waveform...")
    for size in sizes:
        png_path = iconset_dir / f"icon_{size}x{size}.png"
        
        # Create the image
        img = create_icon_image(size)
        img.save(str(png_path))
        print(f"  Created {size}x{size} PNG")
    
    # Use iconutil to create ICNS
    print("Creating ICNS file...")
    cmd = ["iconutil", "-c", "icns", "-o", str(icns_file), str(iconset_dir)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode == 0:
        print(f"✓ Successfully created: {icns_file}")
        # Clean up
        import shutil
        shutil.rmtree(iconset_dir)
        print("✓ Cleaned up temporary files")
        return True
    else:
        print(f"✗ Error creating ICNS: {result.stderr}")
        return False

if __name__ == "__main__":
    try:
        success = create_macos_icon()
        if not success:
            print("\n⚠ Failed to create ICNS, but PNG files are available in the iconset")
    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
