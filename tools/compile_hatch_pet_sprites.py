#!/usr/bin/env python3
"""
Hatch-Pet Sprite Atlas & 480x480 Polaroid UI Compiler (macOS CoreGraphics + CoreText).

Pipeline:
1. Reads the 4x4 magenta (#FF00FF) chroma-key sprite sheets generated for our 9 kid/teen-friendly
   3D-toy blind-box mascot pets:
     0: nebula_dragon (星云小龙 · 星宝)
     1: cloud_puff    (棉花糖云 · 绵绵)
     2: mochi_fox     (草莓麻薯狐 · 莓莓)
     3: capybara      (泡芙水豚 · 佛系橘)
     4: astro_penguin (宇航企鹅 · 啵啵)
     5: chibi_rex     (呆萌小恐龙 · 咔咔)
     6: volt_robo     (雷电机甲 · 闪闪)
     7: aurora_dragon (极光星龙 · 幻幻 - pastel ice-aurora variant)
     8: sakura_fox    (樱花灵狐 · 桃桃 - sakura-blossom variant)
2. Applies hatch-pet chroma-key removal (#FF00FF + despill + edge cleanup) and per-cell
   centroid horizontal alignment & bottom grounding into clean 4x4 transparent RGBA PNG atlases
   (`apps/guardian/assets/pets/pet_0..8_atlas.png`) for Guardian Web.
3. Compiles clean pet-free habitat stage backgrounds (Sunny Forest Room & Starry Manor Room &
   Runaway Empty Nest with farewell letter), 9x16 sprite frames (68x68 RGB565+Alpha4bit packed),
   3D food icons, and crisp TrueType Chinese UI glyphs into `yami_pet_sprites.h` (< 2.5 MB Flash!).
"""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = Path("/Users/thinkmo/.gemini/antigravity/brain/b648d34d-9537-4e1a-a425-702f61985bae")
WEB_PETS_DIR = ROOT / "apps" / "guardian" / "assets" / "pets"
WEB_ANIME_DIR = ROOT / "apps" / "guardian" / "assets" / "anime"
FW_UI_DIR = ROOT / "firmware" / "esp-mosaico-vibe" / "projects" / "yami" / "components" / "app_hello" / "ui"
FW_FLUENT_DIR = FW_UI_DIR / "assets" / "fluent3d"
FONT_BOLD = FW_UI_DIR / "fonts" / "YamiCN-Bold.ttc"
OUT_HEADER = ROOT / "firmware" / "esp-mosaico-vibe" / "projects" / "yami" / "components" / "app_hello" / "yami_pet_sprites.h"

OBJC_SOURCE = r'''
#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <CoreText/CoreText.h>
#import <ImageIO/ImageIO.h>
#import <math.h>

static CGImageRef load_image(NSString *path) {
    NSURL *url = [NSURL fileURLWithPath:path];
    CGImageSourceRef src = CGImageSourceCreateWithURL((__bridge CFURLRef)url, NULL);
    if (!src) return NULL;
    CGImageRef img = CGImageSourceCreateImageAtIndex(src, 0, NULL);
    CFRelease(src);
    return img;
}

static void save_png(CGImageRef img, NSString *path) {
    NSURL *url = [NSURL fileURLWithPath:path];
    CGImageDestinationRef dst = CGImageDestinationCreateWithURL((__bridge CFURLRef)url, CFSTR("public.png"), 1, NULL);
    if (!dst) return;
    CGImageDestinationAddImage(dst, img, NULL);
    CGImageDestinationFinalize(dst);
    CFRelease(dst);
}

static CGContextRef create_rgba_ctx(int w, int h) {
    CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();
    CGContextRef ctx = CGBitmapContextCreate(NULL, w, h, 8, w * 4, cs,
                                             kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big);
    CGColorSpaceRelease(cs);
    CGContextSetInterpolationQuality(ctx, kCGInterpolationHigh);
    return ctx;
}

static uint16_t to_rgb565(uint8_t r, uint8_t g, uint8_t b) {
    return (uint16_t)((((uint16_t)r & 0xF8) << 8) | (((uint16_t)g & 0xFC) << 3) | ((uint16_t)b >> 3));
}

/* Remove #FF00FF magenta chroma-key with smooth feathering & magenta spill suppression */
static void remove_magenta_chroma(uint8_t *px, int w, int h, int tint_mode) {
    for (int i = 0; i < w * h; ++i) {
        uint8_t r = px[i * 4 + 0];
        uint8_t g = px[i * 4 + 1];
        uint8_t b = px[i * 4 + 2];

        /* Distance to pure magenta (255, 0, 255) and magenta-ness score */
        float dr = (float)r - 255.0f;
        float dg = (float)g;
        float db = (float)b - 255.0f;
        float dist = sqrtf(dr * dr + dg * dg + db * db);
        int mag_score = ((int)r + (int)b) / 2 - (int)g;

        if (dist < 118.0f || (mag_score > 78 && r > 145 && b > 145 && g < 110)) {
            px[i * 4 + 0] = 0;
            px[i * 4 + 1] = 0;
            px[i * 4 + 2] = 0;
            px[i * 4 + 3] = 0;
            continue;
        }
        /* Anti-aliased halo feather & despill on boundary pixels */
        uint8_t a = 255;
        if (dist < 156.0f && mag_score > 45) {
            float t = (dist - 118.0f) / 38.0f;
            if (t < 0.0f) t = 0.0f;
            if (t > 1.0f) t = 1.0f;
            a = (uint8_t)(t * 255.0f);
            /* Despill magenta fringes by clamping R & B toward G */
            int max_rb = (int)g + 28;
            if (r > max_rb) r = (uint8_t)((r + max_rb) / 2);
            if (b > max_rb) b = (uint8_t)((b + max_rb) / 2);
        }

        /* Optional collector-edition colorway shift for pets 7 (Aurora Dragon) & 8 (Sakura Fox) */
        if (tint_mode == 1) {
            /* Mint-green -> Ice Aurora Blue-Lavender */
            uint8_t nr = (uint8_t)MIN(255, (int)(r * 0.85f + b * 0.25f));
            uint8_t ng = (uint8_t)MIN(255, (int)(g * 0.86f + 16));
            uint8_t nb = (uint8_t)MIN(255, (int)(b * 0.65f + g * 0.55f + 22));
            r = nr; g = ng; b = nb;
        } else if (tint_mode == 2) {
            /* Cream-peach fox -> Sakura Pink-Berry Fox */
            uint8_t nr = (uint8_t)MIN(255, (int)(r * 1.04f + 10));
            uint8_t ng = (uint8_t)MIN(255, (int)(g * 0.86f));
            uint8_t nb = (uint8_t)MIN(255, (int)(b * 0.96f + 18));
            r = nr; g = ng; b = nb;
        }

        /* Premultiply for CoreGraphics RGBA */
        px[i * 4 + 0] = (uint8_t)((int)r * (int)a / 255);
        px[i * 4 + 1] = (uint8_t)((int)g * (int)a / 255);
        px[i * 4 + 2] = (uint8_t)((int)b * (int)a / 255);
        px[i * 4 + 3] = a;
    }

    /* Strip 3px border of each 4x4 cell to eliminate any faint divider lines */
    int cw = w / 4, ch = h / 4;
    for (int row = 0; row < 4; ++row) {
        for (int col = 0; col < 4; ++col) {
            int x0 = col * cw, y0 = row * ch;
            for (int dy = 0; dy < ch; ++dy) {
                for (int dx = 0; dx < cw; ++dx) {
                    if (dx < 3 || dx >= cw - 3 || dy < 3 || dy >= ch - 3) {
                        int idx = ((y0 + dy) * w + (x0 + dx)) * 4;
                        px[idx + 0] = 0; px[idx + 1] = 0; px[idx + 2] = 0; px[idx + 3] = 0;
                    }
                }
            }
        }
    }
}

/* Extract 4x4 cells, compute centroid X & bottom Y, align into clean transparent 4x4 atlas
   (512x512 for Web PNG, and 16 frames of 68x68 packed RGB565+Alpha4 for Mosaico firmware) */
static void process_pet_sheet(NSMutableString *hdr, int pet_idx, NSString *srcPath,
                              NSString *outWebAtlasPath, int tint_mode) {
    CGImageRef src = load_image(srcPath);
    if (!src) {
        fprintf(stderr, "ERROR: failed to load %s\n", [srcPath UTF8String]);
        return;
    }
    int sw = 768, sh = 768;
    CGContextRef sctx = create_rgba_ctx(sw, sh);
    CGContextDrawImage(sctx, CGRectMake(0, 0, sw, sh), src);
    CGImageRelease(src);

    uint8_t *spx = (uint8_t *)CGBitmapContextGetData(sctx);
    remove_magenta_chroma(spx, sw, sh, tint_mode);

    /* Web atlas: 4x4 of 128x128 = 512x512 */
    int wcell = 128;
    CGContextRef wctx = create_rgba_ctx(wcell * 4, wcell * 4);
    CGImageRef cleanSheet = CGBitmapContextCreateImage(sctx);

    /* Firmware sprite frame size: 68x68 (4624 px/frame, 16 frames = 73,984 px -> 222 KB raw,
       we store RGB565 (uint16_t) + 4-bit Alpha (2 pixels/byte) = 185 KB/pet; 9 pets = ~1.66 MB!) */
    const int FW_S = 68;
    const int FW_CELL_PX = FW_S * FW_S;

    [hdr appendFormat:@"static const uint16_t pet_%d_frames_rgb[16][%d] = {\n", pet_idx, FW_CELL_PX];
    NSMutableString *alphaBuf = [NSMutableString stringWithCapacity:200000];
    [alphaBuf appendFormat:@"static const uint8_t pet_%d_frames_a4[16][%d] = {\n", pet_idx, FW_CELL_PX / 2];

    int cw = sw / 4, ch = sh / 4;
    for (int row = 0; row < 4; ++row) {
        for (int col = 0; col < 4; ++col) {
            int fidx = row * 4 + col;
            int bx0 = col * cw, by0 = row * ch;

            /* Find tight bounding box & horizontal centroid of opaque pixels */
            int minx = cw, maxx = -1, miny = ch, maxy = -1;
            double sum_x = 0.0, sum_a = 0.0;
            for (int y = 0; y < ch; ++y) {
                for (int x = 0; x < cw; ++x) {
                    uint8_t a = spx[((by0 + y) * sw + (bx0 + x)) * 4 + 3];
                    if (a > 24) {
                        if (x < minx) minx = x;
                        if (x > maxx) maxx = x;
                        if (y < miny) miny = y;
                        if (y > maxy) maxy = y;
                        sum_x += (double)x * (double)a;
                        sum_a += (double)a;
                    }
                }
            }
            if (maxx < minx) { minx = 8; maxx = cw - 8; miny = 8; maxy = ch - 8; }
            int crop_w = maxx - minx + 1;
            int crop_h = maxy - miny + 1;
            double centroid_x = (sum_a > 0) ? (sum_x / sum_a - minx) : (crop_w * 0.5);

            /* Crop tightly from cleanSheet (CoreGraphics crop uses top-left origin) */
            CGRect cropR = CGRectMake(bx0 + minx, by0 + miny, crop_w, crop_h);
            CGImageRef frameImg = CGImageCreateWithImageInRect(cleanSheet, cropR);

            /* 1) Draw into Web 512x512 atlas (bottom-grounded at 92% of cell height, centroid centered) */
            double wscale = fmin((wcell * 0.86) / crop_w, (wcell * 0.86) / crop_h);
            double dw = crop_w * wscale;
            double dh = crop_h * wscale;
            double wx = col * wcell + (wcell * 0.5 - centroid_x * wscale);
            /* CoreGraphics Y is bottom-up */
            double wy = (3 - row) * wcell + (wcell * 0.06);
            CGContextDrawImage(wctx, CGRectMake(wx, wy, dw, dh), frameImg);

            /* 2) Render into 68x68 firmware frame buffer */
            CGContextRef fctx = create_rgba_ctx(FW_S, FW_S);
            double fscale = fmin((FW_S * 0.90) / crop_w, (FW_S * 0.90) / crop_h);
            double fdw = crop_w * fscale;
            double fdh = crop_h * fscale;
            double fx = (FW_S * 0.5 - centroid_x * fscale);
            double fy = 2.0; /* bottom-grounded */
            CGContextDrawImage(fctx, CGRectMake(fx, fy, fdw, fdh), frameImg);
            uint8_t *fpx = (uint8_t *)CGBitmapContextGetData(fctx);

            [hdr appendString:@"  {"];
            [alphaBuf appendString:@"  {"];
            for (int k = 0; k < FW_CELL_PX; ++k) {
                uint8_t a = fpx[k * 4 + 3];
                uint8_t r = fpx[k * 4 + 0];
                uint8_t g = fpx[k * 4 + 1];
                uint8_t b = fpx[k * 4 + 2];
                if (a > 0) {
                    r = (uint8_t)MIN(255, (int)r * 255 / (int)a);
                    g = (uint8_t)MIN(255, (int)g * 255 / (int)a);
                    b = (uint8_t)MIN(255, (int)b * 255 / (int)a);
                }
                [hdr appendFormat:@"0x%04x%@", to_rgb565(r, g, b), (k + 1 < FW_CELL_PX) ? @"," : @""];
                if ((k & 1) == 1) {
                    uint8_t a_prev = fpx[(k - 1) * 4 + 3] >> 4;
                    uint8_t a_cur  = a >> 4;
                    uint8_t packed = (uint8_t)((a_prev << 4) | a_cur);
                    [alphaBuf appendFormat:@"%u%@", packed, (k + 1 < FW_CELL_PX) ? @"," : @""];
                }
            }
            [hdr appendFormat:@"}%@\n", (fidx < 15) ? @"," : @""];
            [alphaBuf appendFormat:@"}%@\n", (fidx < 15) ? @"," : @""];

            CGContextRelease(fctx);
            CGImageRelease(frameImg);
        }
    }
    [hdr appendString:@"};\n"];
    [alphaBuf appendString:@"};\n"];
    [hdr appendString:alphaBuf];

    CGImageRef webAtlas = CGBitmapContextCreateImage(wctx);
    save_png(webAtlas, outWebAtlasPath);
    CGImageRelease(webAtlas);
    CGImageRelease(cleanSheet);
    CGContextRelease(wctx);
    CGContextRelease(sctx);
}

/* Paint 4 progressively richer, pet-free Habitat Room stages (lv=0..3 for Lv.1..Lv.4) */
static void paint_habitat_room(CGContextRef ctx, int w, int h, int lv) {
    CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();

    if (lv == 0) {
        /* Lv.1: Cozy Sunlit Cream Studio */
        CGFloat wallColors[] = {0.96f, 0.94f, 0.87f, 1.0f,  0.89f, 0.91f, 0.83f, 1.0f};
        CGGradientRef wg = CGGradientCreateWithColorComponents(cs, wallColors, NULL, 2);
        CGContextDrawLinearGradient(ctx, wg, CGPointMake(0, h), CGPointMake(0, h * 0.34f), 0);
        CGGradientRelease(wg);

        CGFloat floorColors[] = {0.84f, 0.70f, 0.52f, 1.0f,  0.71f, 0.55f, 0.37f, 1.0f};
        CGGradientRef fg = CGGradientCreateWithColorComponents(cs, floorColors, NULL, 2);
        CGContextSaveGState(ctx);
        CGContextClipToRect(ctx, CGRectMake(0, 0, w, h * 0.35f));
        CGContextDrawLinearGradient(ctx, fg, CGPointMake(0, h * 0.35f), CGPointMake(0, 0), 0);
        CGContextRestoreGState(ctx);
        CGGradientRelease(fg);

        CGContextSetRGBFillColor(ctx, 0.94f, 0.90f, 0.82f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(0, h * 0.34f, w, h * 0.042f));

        /* Compact square window */
        float wx = w * 0.30f, wy = h * 0.48f, ww = w * 0.40f, wh = h * 0.40f;
        CGFloat skyColors[] = {0.66f, 0.87f, 0.98f, 1.0f,  0.91f, 0.97f, 0.92f, 1.0f};
        CGGradientRef sg = CGGradientCreateWithColorComponents(cs, skyColors, NULL, 2);
        CGContextSaveGState(ctx);
        CGPathRef winPath = CGPathCreateWithRoundedRect(CGRectMake(wx, wy, ww, wh), 10, 10, NULL);
        CGContextAddPath(ctx, winPath);
        CGContextClip(ctx);
        CGContextDrawLinearGradient(ctx, sg, CGPointMake(0, wy + wh), CGPointMake(0, wy), 0);
        CGContextSetRGBFillColor(ctx, 0.46f, 0.74f, 0.44f, 0.86f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx - 20, wy - 20, ww * 0.65f, wh * 0.46f));
        CGContextFillEllipseInRect(ctx, CGRectMake(wx + ww * 0.42f, wy - 24, ww * 0.70f, wh * 0.50f));
        CGContextRestoreGState(ctx);
        CGGradientRelease(sg);

        CGContextSetRGBStrokeColor(ctx, 0.98f, 0.96f, 0.91f, 1.0f);
        CGContextSetLineWidth(ctx, 6.0f);
        CGContextAddPath(ctx, winPath);
        CGContextStrokePath(ctx);
        CGContextMoveToPoint(ctx, wx + ww * 0.5f, wy);
        CGContextAddLineToPoint(ctx, wx + ww * 0.5f, wy + wh);
        CGContextMoveToPoint(ctx, wx, wy + wh * 0.5f);
        CGContextAddLineToPoint(ctx, wx + ww, wy + wh * 0.5f);
        CGContextStrokePath(ctx);
        CGPathRelease(winPath);

        /* Simple round cream rug */
        CGContextSetRGBFillColor(ctx, 0.95f, 0.90f, 0.80f, 0.92f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.16f, h * 0.06f, w * 0.68f, h * 0.21f));

        /* Small sprout potted plant */
        CGContextSetRGBFillColor(ctx, 0.78f, 0.46f, 0.30f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(w * 0.06f, h * 0.24f, w * 0.09f, h * 0.13f));
        CGContextSetRGBFillColor(ctx, 0.28f, 0.64f, 0.38f, 1.0f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.04f, h * 0.35f, w * 0.11f, h * 0.16f));
    } else if (lv == 1) {
        /* Lv.2: Warm Cedar Wood Cabin (原木小筑) */
        CGFloat wallColors[] = {0.95f, 0.88f, 0.74f, 1.0f,  0.88f, 0.77f, 0.60f, 1.0f};
        CGGradientRef wg = CGGradientCreateWithColorComponents(cs, wallColors, NULL, 2);
        CGContextDrawLinearGradient(ctx, wg, CGPointMake(0, h), CGPointMake(0, h * 0.34f), 0);
        CGGradientRelease(wg);

        /* Subtle vertical cedar wood planks */
        CGContextSetRGBStrokeColor(ctx, 0.78f, 0.64f, 0.45f, 0.32f);
        CGContextSetLineWidth(ctx, 2.0f);
        for (int px = 36; px < w; px += 44) {
            CGContextMoveToPoint(ctx, px, h * 0.35f);
            CGContextAddLineToPoint(ctx, px, h);
        }
        CGContextStrokePath(ctx);

        /* Rich walnut-honey floor */
        CGFloat floorColors[] = {0.79f, 0.61f, 0.40f, 1.0f,  0.65f, 0.47f, 0.28f, 1.0f};
        CGGradientRef fg = CGGradientCreateWithColorComponents(cs, floorColors, NULL, 2);
        CGContextSaveGState(ctx);
        CGContextClipToRect(ctx, CGRectMake(0, 0, w, h * 0.35f));
        CGContextDrawLinearGradient(ctx, fg, CGPointMake(0, h * 0.35f), CGPointMake(0, 0), 0);
        CGContextRestoreGState(ctx);
        CGGradientRelease(fg);

        /* Wooden baseboard */
        CGContextSetRGBFillColor(ctx, 0.62f, 0.44f, 0.26f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(0, h * 0.34f, w, h * 0.045f));

        /* Arched sunny timber window */
        float wx = w * 0.26f, wy = h * 0.44f, ww = w * 0.48f, wh = h * 0.48f;
        CGFloat skyColors[] = {0.56f, 0.83f, 0.98f, 1.0f,  0.96f, 0.94f, 0.82f, 1.0f};
        CGGradientRef sg = CGGradientCreateWithColorComponents(cs, skyColors, NULL, 2);
        CGContextSaveGState(ctx);
        CGPathRef winPath = CGPathCreateWithRoundedRect(CGRectMake(wx, wy, ww, wh), ww * 0.24f, ww * 0.24f, NULL);
        CGContextAddPath(ctx, winPath);
        CGContextClip(ctx);
        CGContextDrawLinearGradient(ctx, sg, CGPointMake(0, wy + wh), CGPointMake(0, wy), 0);
        /* Fluffy clouds & pine hills */
        CGContextSetRGBFillColor(ctx, 1.0f, 1.0f, 1.0f, 0.88f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx + 24, wy + wh * 0.62f, 70, 26));
        CGContextFillEllipseInRect(ctx, CGRectMake(wx + ww - 95, wy + wh * 0.56f, 64, 22));
        CGContextSetRGBFillColor(ctx, 0.32f, 0.64f, 0.38f, 0.90f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx - 25, wy - 20, ww * 0.65f, wh * 0.46f));
        CGContextSetRGBFillColor(ctx, 0.42f, 0.72f, 0.42f, 0.92f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx + ww * 0.36f, wy - 25, ww * 0.70f, wh * 0.50f));
        CGContextRestoreGState(ctx);
        CGGradientRelease(sg);

        CGContextSetRGBStrokeColor(ctx, 0.58f, 0.40f, 0.24f, 1.0f);
        CGContextSetLineWidth(ctx, 8.0f);
        CGContextAddPath(ctx, winPath);
        CGContextStrokePath(ctx);
        CGContextSetLineWidth(ctx, 5.0f);
        CGContextMoveToPoint(ctx, wx + ww * 0.5f, wy);
        CGContextAddLineToPoint(ctx, wx + ww * 0.5f, wy + wh);
        CGContextMoveToPoint(ctx, wx, wy + wh * 0.45f);
        CGContextAddLineToPoint(ctx, wx + ww, wy + wh * 0.45f);
        CGContextStrokePath(ctx);
        CGPathRelease(winPath);

        /* Wall bookshelf with colorful books on right */
        CGContextSetRGBFillColor(ctx, 0.56f, 0.38f, 0.22f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(w * 0.79f, h * 0.62f, w * 0.17f, 10));
        CGContextSetRGBFillColor(ctx, 0.88f, 0.42f, 0.36f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(w * 0.81f, h * 0.64f, 14, 34));
        CGContextSetRGBFillColor(ctx, 0.30f, 0.62f, 0.78f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(w * 0.81f + 16, h * 0.64f, 14, 40));
        CGContextSetRGBFillColor(ctx, 0.94f, 0.74f, 0.26f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(w * 0.81f + 32, h * 0.64f, 14, 30));

        /* Double-ring cozy woven rug */
        CGContextSetRGBFillColor(ctx, 0.96f, 0.89f, 0.76f, 0.95f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.13f, h * 0.05f, w * 0.74f, h * 0.23f));
        CGContextSetRGBStrokeColor(ctx, 0.84f, 0.58f, 0.36f, 0.85f);
        CGContextSetLineWidth(ctx, 4.0f);
        CGContextStrokeEllipseInRect(ctx, CGRectMake(w * 0.16f, h * 0.07f, w * 0.68f, h * 0.19f));

        /* Lush monstera plant on left */
        CGContextSetRGBFillColor(ctx, 0.82f, 0.48f, 0.30f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(w * 0.04f, h * 0.24f, w * 0.11f, h * 0.15f));
        CGContextSetRGBFillColor(ctx, 0.20f, 0.56f, 0.34f, 1.0f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.01f, h * 0.36f, w * 0.14f, h * 0.22f));
        CGContextSetRGBFillColor(ctx, 0.32f, 0.70f, 0.42f, 0.95f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.07f, h * 0.40f, w * 0.13f, h * 0.19f));

        /* Warm diagonal sunbeam */
        CGContextSetRGBFillColor(ctx, 1.0f, 0.94f, 0.72f, 0.18f);
        CGContextMoveToPoint(ctx, wx + ww * 0.18f, wy + wh);
        CGContextAddLineToPoint(ctx, wx + ww * 0.92f, wy + wh);
        CGContextAddLineToPoint(ctx, w * 0.68f, 0);
        CGContextAddLineToPoint(ctx, w * 0.14f, 0);
        CGContextClosePath(ctx);
        CGContextFillPath(ctx);
    } else if (lv == 2) {
        /* Lv.3: Sunlit Blooming Garden Conservatory (阳光庭院) */
        CGFloat wallColors[] = {0.93f, 0.97f, 0.91f, 1.0f,  0.84f, 0.93f, 0.85f, 1.0f};
        CGGradientRef wg = CGGradientCreateWithColorComponents(cs, wallColors, NULL, 2);
        CGContextDrawLinearGradient(ctx, wg, CGPointMake(0, h), CGPointMake(0, h * 0.34f), 0);
        CGGradientRelease(wg);

        /* Warm sunlit terrace deck floor */
        CGFloat floorColors[] = {0.90f, 0.79f, 0.60f, 1.0f,  0.79f, 0.65f, 0.45f, 1.0f};
        CGGradientRef fg = CGGradientCreateWithColorComponents(cs, floorColors, NULL, 2);
        CGContextSaveGState(ctx);
        CGContextClipToRect(ctx, CGRectMake(0, 0, w, h * 0.35f));
        CGContextDrawLinearGradient(ctx, fg, CGPointMake(0, h * 0.35f), CGPointMake(0, 0), 0);
        CGContextRestoreGState(ctx);
        CGGradientRelease(fg);

        CGContextSetRGBFillColor(ctx, 0.98f, 0.96f, 0.90f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(0, h * 0.34f, w, h * 0.042f));

        /* Panoramic triple-pane French garden windows */
        float wx = w * 0.10f, wy = h * 0.40f, ww = w * 0.80f, wh = h * 0.54f;
        CGFloat skyColors[] = {0.50f, 0.84f, 0.99f, 1.0f,  0.94f, 0.98f, 0.88f, 1.0f};
        CGGradientRef sg = CGGradientCreateWithColorComponents(cs, skyColors, NULL, 2);
        CGContextSaveGState(ctx);
        CGPathRef winPath = CGPathCreateWithRoundedRect(CGRectMake(wx, wy, ww, wh), 16, 16, NULL);
        CGContextAddPath(ctx, winPath);
        CGContextClip(ctx);
        CGContextDrawLinearGradient(ctx, sg, CGPointMake(0, wy + wh), CGPointMake(0, wy), 0);
        /* Golden sun glow */
        CGContextSetRGBFillColor(ctx, 1.0f, 0.95f, 0.68f, 0.72f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx + ww * 0.72f, wy + wh * 0.62f, 66, 66));
        /* Rolling lush hills */
        CGContextSetRGBFillColor(ctx, 0.38f, 0.74f, 0.42f, 0.90f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx - 30, wy - 30, ww * 0.55f, wh * 0.52f));
        CGContextSetRGBFillColor(ctx, 0.48f, 0.80f, 0.46f, 0.92f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx + ww * 0.38f, wy - 35, ww * 0.68f, wh * 0.56f));
        /* Blooming pink & lavender hydrangea clusters outside window */
        float fx[] = {0.14f, 0.24f, 0.76f, 0.84f, 0.52f};
        float fy[] = {0.20f, 0.14f, 0.22f, 0.12f, 0.10f};
        for (int i = 0; i < 5; ++i) {
            if (i % 2 == 0) CGContextSetRGBFillColor(ctx, 0.98f, 0.62f, 0.74f, 0.92f);
            else            CGContextSetRGBFillColor(ctx, 0.72f, 0.66f, 0.96f, 0.92f);
            CGContextFillEllipseInRect(ctx, CGRectMake(wx + ww * fx[i], wy + wh * fy[i], 38, 34));
            CGContextFillEllipseInRect(ctx, CGRectMake(wx + ww * fx[i] + 16, wy + wh * fy[i] + 10, 32, 28));
        }
        CGContextRestoreGState(ctx);
        CGGradientRelease(sg);

        /* White conservatory French window mullions */
        CGContextSetRGBStrokeColor(ctx, 0.99f, 0.98f, 0.95f, 1.0f);
        CGContextSetLineWidth(ctx, 7.0f);
        CGContextAddPath(ctx, winPath);
        CGContextStrokePath(ctx);
        CGContextMoveToPoint(ctx, wx + ww * 0.33f, wy);
        CGContextAddLineToPoint(ctx, wx + ww * 0.33f, wy + wh);
        CGContextMoveToPoint(ctx, wx + ww * 0.67f, wy);
        CGContextAddLineToPoint(ctx, wx + ww * 0.67f, wy + wh);
        CGContextMoveToPoint(ctx, wx, wy + wh * 0.46f);
        CGContextAddLineToPoint(ctx, wx + ww, wy + wh * 0.46f);
        CGContextStrokePath(ctx);
        CGPathRelease(winPath);

        /* Hanging ivy leaf garland along top */
        for (int i = 0; i < 11; ++i) {
            float lx = w * 0.08f + i * (w * 0.082f);
            float ly = h * 0.90f - sinf((float)i * 0.7f) * 14.0f;
            CGContextSetRGBFillColor(ctx, 0.26f, 0.66f, 0.38f, 0.92f);
            CGContextFillEllipseInRect(ctx, CGRectMake(lx, ly, 26, 16));
        }

        /* Sage-and-cream woven garden rug */
        CGContextSetRGBFillColor(ctx, 0.88f, 0.94f, 0.84f, 0.94f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.12f, h * 0.05f, w * 0.76f, h * 0.24f));
        CGContextSetRGBStrokeColor(ctx, 0.46f, 0.72f, 0.52f, 0.82f);
        CGContextSetLineWidth(ctx, 3.5f);
        CGContextStrokeEllipseInRect(ctx, CGRectMake(w * 0.15f, h * 0.07f, w * 0.70f, h * 0.20f));

        /* Blooming flower pot on left */
        CGContextSetRGBFillColor(ctx, 0.88f, 0.56f, 0.36f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(w * 0.03f, h * 0.24f, w * 0.11f, h * 0.15f));
        CGContextSetRGBFillColor(ctx, 0.24f, 0.62f, 0.36f, 1.0f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.01f, h * 0.35f, w * 0.15f, h * 0.22f));
        CGContextSetRGBFillColor(ctx, 0.99f, 0.64f, 0.76f, 1.0f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.03f, h * 0.48f, 24, 24));
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.09f, h * 0.44f, 22, 22));

        /* Radiant golden sunbeam */
        CGContextSetRGBFillColor(ctx, 1.0f, 0.97f, 0.76f, 0.20f);
        CGContextMoveToPoint(ctx, wx + ww * 0.25f, wy + wh);
        CGContextAddLineToPoint(ctx, wx + ww * 0.95f, wy + wh);
        CGContextAddLineToPoint(ctx, w * 0.72f, 0);
        CGContextAddLineToPoint(ctx, w * 0.18f, 0);
        CGContextClosePath(ctx);
        CGContextFillPath(ctx);
    } else {
        /* Lv.4: Magical Starlit Observatory Manor (星空庄园) */
        CGFloat wallColors[] = {0.14f, 0.17f, 0.34f, 1.0f,  0.22f, 0.24f, 0.44f, 1.0f};
        CGGradientRef wg = CGGradientCreateWithColorComponents(cs, wallColors, NULL, 2);
        CGContextDrawLinearGradient(ctx, wg, CGPointMake(0, h), CGPointMake(0, h * 0.34f), 0);
        CGGradientRelease(wg);

        /* Polished twilight mahogany floor */
        CGFloat floorColors[] = {0.34f, 0.26f, 0.40f, 1.0f,  0.22f, 0.17f, 0.30f, 1.0f};
        CGGradientRef fg = CGGradientCreateWithColorComponents(cs, floorColors, NULL, 2);
        CGContextSaveGState(ctx);
        CGContextClipToRect(ctx, CGRectMake(0, 0, w, h * 0.35f));
        CGContextDrawLinearGradient(ctx, fg, CGPointMake(0, h * 0.35f), CGPointMake(0, 0), 0);
        CGContextRestoreGState(ctx);
        CGGradientRelease(fg);

        /* Gold-trimmed royal molding */
        CGContextSetRGBFillColor(ctx, 0.88f, 0.74f, 0.40f, 0.90f);
        CGContextFillRect(ctx, CGRectMake(0, h * 0.34f, w, h * 0.032f));

        /* Grand arched starlight observatory window */
        float wx = w * 0.16f, wy = h * 0.40f, ww = w * 0.68f, wh = h * 0.56f;
        CGFloat skyColors[] = {0.06f, 0.08f, 0.24f, 1.0f,  0.22f, 0.26f, 0.56f, 1.0f};
        CGGradientRef sg = CGGradientCreateWithColorComponents(cs, skyColors, NULL, 2);
        CGContextSaveGState(ctx);
        CGPathRef winPath = CGPathCreateWithRoundedRect(CGRectMake(wx, wy, ww, wh), ww * 0.28f, ww * 0.28f, NULL);
        CGContextAddPath(ctx, winPath);
        CGContextClip(ctx);
        CGContextDrawLinearGradient(ctx, sg, CGPointMake(0, wy + wh), CGPointMake(0, wy), 0);

        /* Nebula aurora ribbon inside window */
        CGContextSetRGBFillColor(ctx, 0.52f, 0.40f, 0.90f, 0.32f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx - 20, wy + wh * 0.22f, ww * 0.90f, wh * 0.46f));
        CGContextSetRGBFillColor(ctx, 0.32f, 0.78f, 0.88f, 0.24f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx + ww * 0.25f, wy + wh * 0.42f, ww * 0.75f, wh * 0.36f));

        /* Glowing golden crescent moon */
        CGContextSetRGBFillColor(ctx, 1.0f, 0.94f, 0.64f, 1.0f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx + ww * 0.66f, wy + wh * 0.60f, 52, 52));
        CGContextSetRGBFillColor(ctx, 0.14f, 0.18f, 0.42f, 1.0f);
        CGContextFillEllipseInRect(ctx, CGRectMake(wx + ww * 0.66f + 16, wy + wh * 0.60f + 8, 44, 44));

        /* Twinkling stars & shooting star */
        for (int s = 0; s < 26; ++s) {
            float sx = wx + 18 + (float)((s * 73 + 29) % (int)(ww - 36));
            float sy = wy + 22 + (float)((s * 47 + 17) % (int)(wh - 44));
            float sr = (s % 4 == 0) ? 5.5f : 3.2f;
            CGContextSetRGBFillColor(ctx, 1.0f, 0.96f, 0.78f, (s % 3 == 0) ? 0.96f : 0.72f);
            CGContextFillEllipseInRect(ctx, CGRectMake(sx, sy, sr, sr));
        }
        CGContextSetRGBStrokeColor(ctx, 1.0f, 0.96f, 0.74f, 0.85f);
        CGContextSetLineWidth(ctx, 2.5f);
        CGContextMoveToPoint(ctx, wx + ww * 0.20f, wy + wh * 0.78f);
        CGContextAddLineToPoint(ctx, wx + ww * 0.42f, wy + wh * 0.64f);
        CGContextStrokePath(ctx);
        CGContextRestoreGState(ctx);
        CGGradientRelease(sg);

        /* Golden palace observatory arch frame */
        CGContextSetRGBStrokeColor(ctx, 0.92f, 0.78f, 0.44f, 1.0f);
        CGContextSetLineWidth(ctx, 7.0f);
        CGContextAddPath(ctx, winPath);
        CGContextStrokePath(ctx);
        CGContextSetLineWidth(ctx, 4.0f);
        CGContextMoveToPoint(ctx, wx + ww * 0.5f, wy);
        CGContextAddLineToPoint(ctx, wx + ww * 0.5f, wy + wh);
        CGContextMoveToPoint(ctx, wx, wy + wh * 0.42f);
        CGContextAddLineToPoint(ctx, wx + ww, wy + wh * 0.42f);
        CGContextStrokePath(ctx);
        CGPathRelease(winPath);

        /* Warm glowing fairy-light string garlands draped across top */
        for (int i = 0; i < 13; ++i) {
            float fx = w * 0.05f + i * (w * 0.075f);
            float fy = h * 0.92f - sinf((float)i * 0.52f) * 20.0f;
            CGContextSetRGBFillColor(ctx, 1.0f, 0.92f, 0.56f, 0.32f);
            CGContextFillEllipseInRect(ctx, CGRectMake(fx - 10, fy - 10, 28, 28));
            CGContextSetRGBFillColor(ctx, 1.0f, 0.97f, 0.78f, 0.98f);
            CGContextFillEllipseInRect(ctx, CGRectMake(fx, fy, 9, 9));
        }

        /* Plush royal cream-and-gold manor rug */
        CGContextSetRGBFillColor(ctx, 0.95f, 0.90f, 0.80f, 0.94f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.11f, h * 0.05f, w * 0.78f, h * 0.24f));
        CGContextSetRGBStrokeColor(ctx, 0.92f, 0.76f, 0.36f, 0.92f);
        CGContextSetLineWidth(ctx, 4.5f);
        CGContextStrokeEllipseInRect(ctx, CGRectMake(w * 0.14f, h * 0.07f, w * 0.72f, h * 0.20f));

        /* Warm glowing star crystal lamp on left */
        CGContextSetRGBFillColor(ctx, 0.88f, 0.74f, 0.40f, 1.0f);
        CGContextFillRect(ctx, CGRectMake(w * 0.07f, h * 0.24f, w * 0.04f, h * 0.22f));
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.04f, h * 0.22f, w * 0.10f, h * 0.05f));
        CGContextSetRGBFillColor(ctx, 1.0f, 0.92f, 0.58f, 0.30f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.01f, h * 0.39f, w * 0.16f, h * 0.22f));
        CGContextSetRGBFillColor(ctx, 1.0f, 0.95f, 0.72f, 0.96f);
        CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.05f, h * 0.44f, w * 0.08f, h * 0.12f));
    }

    /* Ceramic / Royal Pet Food Bowl on the right side of the rug (matches BOWL_X / BOWL_Y) */
    float bx = w * 0.76f, by = h * 0.12f, bw = w * 0.15f, bh = h * 0.10f;
    if (lv == 3)      CGContextSetRGBFillColor(ctx, 0.88f, 0.70f, 0.28f, 1.0f); /* Royal Gold Bowl */
    else if (lv == 2) CGContextSetRGBFillColor(ctx, 0.30f, 0.66f, 0.46f, 1.0f); /* Garden Emerald Bowl */
    else if (lv == 1) CGContextSetRGBFillColor(ctx, 0.84f, 0.46f, 0.34f, 1.0f); /* Cedar Terracotta Bowl */
    else              CGContextSetRGBFillColor(ctx, 0.20f, 0.54f, 0.44f, 1.0f); /* Classic Teal Bowl */
    CGPathRef bowlPath = CGPathCreateWithRoundedRect(CGRectMake(bx, by, bw, bh * 0.75f), 8, 8, NULL);
    CGContextAddPath(ctx, bowlPath);
    CGContextFillPath(ctx);
    CGPathRelease(bowlPath);
    CGContextSetRGBFillColor(ctx, 0.96f, 0.96f, 0.94f, 1.0f);
    CGContextFillEllipseInRect(ctx, CGRectMake(bx - 2, by + bh * 0.52f, bw + 4, bh * 0.38f));
    CGContextSetRGBFillColor(ctx, 0.78f, 0.54f, 0.28f, 1.0f);
    CGContextFillEllipseInRect(ctx, CGRectMake(bx + 4, by + bh * 0.58f, bw - 8, bh * 0.24f));

    CGColorSpaceRelease(cs);
}

static void emit_scene_rgb565(NSMutableString *out, NSString *c_name, CGImageRef img, int w, int h) {
    CGContextRef ctx = create_rgba_ctx(w, h);
    CGContextDrawImage(ctx, CGRectMake(0, 0, w, h), img);
    uint8_t *px = (uint8_t *)CGBitmapContextGetData(ctx);
    [out appendFormat:@"static const uint16_t %@[%d] = {", c_name, w * h];
    for (int i = 0; i < w * h; ++i) {
        [out appendFormat:@"0x%04x%@", to_rgb565(px[i*4], px[i*4+1], px[i*4+2]), (i + 1 < w * h) ? @"," : @""];
    }
    [out appendString:@"};\n"];
    CGContextRelease(ctx);
}

static void emit_sprite_rgba(NSMutableString *out, NSString *c_name, NSString *path, int w, int h) {
    CGImageRef src = load_image(path);
    CGContextRef ctx = create_rgba_ctx(w, h);
    if (src) { CGContextDrawImage(ctx, CGRectMake(0, 0, w, h), src); CGImageRelease(src); }
    uint8_t *px = (uint8_t *)CGBitmapContextGetData(ctx);
    [out appendFormat:@"static const uint16_t %@_rgb[%d] = {", c_name, w * h];
    for (int i = 0; i < w * h; ++i) {
        uint8_t a = px[i*4+3], r = px[i*4], g = px[i*4+1], b = px[i*4+2];
        if (a > 0) {
            r = (uint8_t)MIN(255, (int)r * 255 / (int)a);
            g = (uint8_t)MIN(255, (int)g * 255 / (int)a);
            b = (uint8_t)MIN(255, (int)b * 255 / (int)a);
        }
        [out appendFormat:@"0x%04x%@", to_rgb565(r, g, b), (i + 1 < w * h) ? @"," : @""];
    }
    [out appendString:@"};\n"];
    [out appendFormat:@"static const uint8_t %@_a[%d] = {", c_name, w * h];
    for (int i = 0; i < w * h; ++i) [out appendFormat:@"%u%@", px[i*4+3], (i + 1 < w * h) ? @"," : @""];
    [out appendString:@"};\n"];
    [out appendFormat:@"static const yami_sprite3d_t %@ = {%d, %d, %@_rgb, %@_a};\n", c_name, w, h, c_name, c_name];
    CGContextRelease(ctx);
}

static CTFontRef load_custom_font(NSString *fontPath, CGFloat size) {
    NSURL *url = [NSURL fileURLWithPath:fontPath];
    CFArrayRef descs = CTFontManagerCreateFontDescriptorsFromURL((__bridge CFURLRef)url);
    if (descs && CFArrayGetCount(descs) > 0) {
        CTFontDescriptorRef desc = (CTFontDescriptorRef)CFArrayGetValueAtIndex(descs, 0);
        CTFontRef f = CTFontCreateWithFontDescriptor(desc, size, NULL);
        CFRelease(descs);
        return f;
    }
    if (descs) CFRelease(descs);
    return CTFontCreateWithName(CFSTR("PingFangSC-Semibold"), size, NULL);
}

static void emit_text_glyph(NSMutableString *out, NSString *c_name, NSString *text, CTFontRef font) {
    NSDictionary *attrs = @{
        (__bridge NSString *)kCTFontAttributeName: (__bridge id)font,
        (__bridge NSString *)kCTForegroundColorFromContextAttributeName: @YES
    };
    NSAttributedString *attrStr = [[NSAttributedString alloc] initWithString:text attributes:attrs];
    CTLineRef line = CTLineCreateWithAttributedString((__bridge CFAttributedStringRef)attrStr);
    CGFloat ascent = 0, descent = 0, leading = 0;
    double width = CTLineGetTypographicBounds(line, &ascent, &descent, &leading);
    int w = (int)ceil(width) + 4;
    int h = (int)ceil(ascent + descent) + 4;
    if (w < 4) w = 4;
    if (h < 4) h = 4;

    CGContextRef ctx = create_rgba_ctx(w, h);
    CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0);
    CGContextSetTextPosition(ctx, 2.0, descent + 2.0);
    CTLineDraw(line, ctx);
    CFRelease(line);

    uint8_t *px = (uint8_t *)CGBitmapContextGetData(ctx);
    [out appendFormat:@"static const uint8_t %@_a[%d] = {", c_name, w * h];
    for (int i = 0; i < w * h; ++i) [out appendFormat:@"%u%@", px[i*4+3], (i + 1 < w * h) ? @"," : @""];
    [out appendString:@"};\n"];
    [out appendFormat:@"static const yami_glyph_t %@ = {%d, %d, %@_a};\n", c_name, w, h, c_name];
    CGContextRelease(ctx);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 6) return 1;
        NSString *artDir      = [NSString stringWithUTF8String:argv[1]];
        NSString *webPetsDir  = [NSString stringWithUTF8String:argv[2]];
        NSString *webAnimeDir = [NSString stringWithUTF8String:argv[3]];
        NSString *fwFluentDir = [NSString stringWithUTF8String:argv[4]];
        NSString *fontPath    = [NSString stringWithUTF8String:argv[5]];
        NSString *outHeader   = [NSString stringWithUTF8String:argv[6]];

        NSMutableString *hdr = [NSMutableString stringWithCapacity:4 * 1024 * 1024];
        [hdr appendString:@"// Auto-generated Hatch-Pet 4x4 Animated Sprite Atlases (9 Pets x 16 Frames) + Clean Room Stages + TrueType Glyphs\n"];
        [hdr appendString:@"#pragma once\n#include <stdint.h>\n"];
        [hdr appendString:@"#define YAMI_BG_W 220\n#define YAMI_BG_H 148\n"];
        [hdr appendString:@"#define YAMI_PET_FRAME_S 68\n"];
        [hdr appendString:@"typedef struct { uint16_t w, h; const uint16_t *rgb565; const uint8_t *alpha; } yami_sprite3d_t;\n"];
        [hdr appendString:@"typedef struct { uint16_t w, h; const uint8_t *alpha; } yami_glyph_t;\n\n"];

        /* 1. 4 Progressive Habitat Level Room Backgrounds (Lv.1..Lv.4) + Runaway Empty Nest */
        for (int lv = 0; lv < 4; ++lv) {
            CGContextRef ctx = create_rgba_ctx(660, 444);
            paint_habitat_room(ctx, 660, 444, lv);
            CGImageRef roomImg = CGBitmapContextCreateImage(ctx);
            save_png(roomImg, [webPetsDir stringByAppendingPathComponent:[NSString stringWithFormat:@"stage_lv%d.png", lv + 1]]);
            if (lv == 0) {
                save_png(roomImg, [webPetsDir stringByAppendingPathComponent:@"stage_sunny_room.png"]);
            }
            emit_scene_rgb565(hdr, [NSString stringWithFormat:@"bg_habitat_lv%d_rgb", lv], roomImg, 220, 148);
            CGImageRelease(roomImg);
            CGContextRelease(ctx);
        }
        [hdr appendString:@"static const uint16_t *const bg_habitat_rooms_rgb[4] = {\n"];
        [hdr appendString:@"    bg_habitat_lv0_rgb, bg_habitat_lv1_rgb, bg_habitat_lv2_rgb, bg_habitat_lv3_rgb\n};\n"];
        [hdr appendString:@"#define bg_sunny_room_rgb bg_habitat_lv0_rgb\n"];
        {
            CGImageRef runImg = load_image([webAnimeDir stringByAppendingPathComponent:@"scene_runaway_empty_nest.png"]);
            emit_scene_rgb565(hdr, @"bg_runaway_room_rgb", runImg, 220, 148);
            if (runImg) CGImageRelease(runImg);
        }

        /* 2. Compile all 9 animated 4x4 sprite sheets (chroma-keyed & centroid aligned!) */
        struct {
            NSString *srcName;
            NSString *outName;
            int tint;
        } sheets[9] = {
            {@"pet_0_nebula_dragon_sheet_v2_1795313351389.jpg",   @"pet_0_nebula_dragon_atlas.png", 0},
            {@"pet_1_cloud_puff_sheet_v2_1795313356732.jpg",      @"pet_1_cloud_puff_atlas.png",    0},
            {@"pet_2_mochi_fox_sheet_v2_1795313374876.jpg",       @"pet_2_mochi_fox_atlas.png",     0},
            {@"puff_capybara_spritesheet_4x4_v2_1795313431859.jpg", @"pet_3_capybara_atlas.png",    0},
            {@"astro_penguin_spritesheet_1795313415958.jpg",      @"pet_4_astro_penguin_atlas.png", 0},
            {@"chibi_rex_sprite_sheet_1795313416362.jpg",         @"pet_5_chibi_rex_atlas.png",     0},
            {@"volt_robo_spritesheet_1795313446389.jpg",          @"pet_6_volt_robo_atlas.png",     0},
            {@"pet_0_nebula_dragon_sheet_v2_1795313351389.jpg",   @"pet_7_aurora_dragon_atlas.png", 1},
            {@"pet_2_mochi_fox_sheet_v2_1795313374876.jpg",       @"pet_8_sakura_fox_atlas.png",    2},
        };

        for (int i = 0; i < 9; ++i) {
            process_pet_sheet(hdr, i,
                              [artDir stringByAppendingPathComponent:sheets[i].srcName],
                              [webPetsDir stringByAppendingPathComponent:sheets[i].outName],
                              sheets[i].tint);
        }

        [hdr appendString:@"static const uint16_t (*const pet_all_rgb[9])[4624] = {\n"];
        for (int i = 0; i < 9; ++i) [hdr appendFormat:@"    pet_%d_frames_rgb,\n", i];
        [hdr appendString:@"};\n"];
        [hdr appendString:@"static const uint8_t (*const pet_all_a4[9])[2312] = {\n"];
        for (int i = 0; i < 9; ++i) [hdr appendFormat:@"    pet_%d_frames_a4,\n", i];
        [hdr appendString:@"};\n\n"];

        /* 3. 3D Food & Heart icons */
        emit_sprite_rgba(hdr, @"sp_heart",   [fwFluentDir stringByAppendingPathComponent:@"heart.png"],  34, 34);
        emit_sprite_rgba(hdr, @"sp_bone",    [fwFluentDir stringByAppendingPathComponent:@"bone.png"],   36, 36);
        emit_sprite_rgba(hdr, @"sp_fish",    [fwFluentDir stringByAppendingPathComponent:@"fish.png"],   36, 36);
        emit_sprite_rgba(hdr, @"sp_honey",   [fwFluentDir stringByAppendingPathComponent:@"honey.png"],  36, 36);
        emit_sprite_rgba(hdr, @"sp_cookie",  [fwFluentDir stringByAppendingPathComponent:@"cookie.png"], 36, 36);

        emit_sprite_rgba(hdr, @"ico_bone",   [fwFluentDir stringByAppendingPathComponent:@"bone.png"],   20, 20);
        emit_sprite_rgba(hdr, @"ico_fish",   [fwFluentDir stringByAppendingPathComponent:@"fish.png"],   20, 20);
        emit_sprite_rgba(hdr, @"ico_honey",  [fwFluentDir stringByAppendingPathComponent:@"honey.png"],  20, 20);
        emit_sprite_rgba(hdr, @"ico_cookie", [fwFluentDir stringByAppendingPathComponent:@"cookie.png"], 20, 20);

        /* 4. Pre-baked TrueType Chinese UI glyphs */
        CTFontRef fontTitle = load_custom_font(fontPath, 17.0);
        CTFontRef fontPill  = load_custom_font(fontPath, 12.5);
        CTFontRef fontBar   = load_custom_font(fontPath, 13.0);
        CTFontRef fontBtn   = load_custom_font(fontPath, 13.5);

        NSArray *petTitles = @[
            @"星云小龙•星宝", @"棉花糖云•绵绵", @"麻薯灵狐•莓莓",
            @"泡芙水豚•佛系橘", @"宇航企鹅•啵啵", @"呆萌恐龙•咔咔",
            @"雷电机甲•闪闪", @"极光星龙•幻幻", @"樱花灵狐•桃桃"
        ];
        for (int i = 0; i < 9; ++i) {
            emit_text_glyph(hdr, [NSString stringWithFormat:@"gl_breed_title_%d", i], petTitles[i], fontTitle);
        }
        [hdr appendString:@"static const yami_glyph_t *const gl_breed_titles[9] = {"];
        for (int i = 0; i < 9; ++i) [hdr appendFormat:@"&gl_breed_title_%d%@", i, i < 8 ? @"," : @""];
        [hdr appendString:@"};\n\n"];

        NSArray *habLabels = @[
            @[@"Lv.1 奇幻树屋", @"Lv.2 原木小筑", @"Lv.3 阳光庭院", @"Lv.4 星空庄园"],
            @[@"Lv.1 暖阳小窝", @"Lv.2 原木小筑", @"Lv.3 阳光庭院", @"Lv.4 星空庄园"],
            @[@"Lv.1 迷你基地", @"Lv.2 原木小筑", @"Lv.3 阳光庭院", @"Lv.4 星空庄园"]
        ];
        for (int sp = 0; sp < 3; ++sp) {
            for (int lv = 0; lv < 4; ++lv) {
                emit_text_glyph(hdr, [NSString stringWithFormat:@"gl_hab_%d_%d", sp, lv], habLabels[sp][lv], fontPill);
            }
        }
        [hdr appendString:@"static const yami_glyph_t *const gl_hab_labels[3][4] = {\n"];
        for (int sp = 0; sp < 3; ++sp) {
            [hdr appendFormat:@"    {&gl_hab_%d_0, &gl_hab_%d_1, &gl_hab_%d_2, &gl_hab_%d_3},\n", sp, sp, sp, sp];
        }
        [hdr appendString:@"};\n\n"];

        for (int d = 0; d <= 9; ++d) {
            emit_text_glyph(hdr, [NSString stringWithFormat:@"gl_dig_%d", d], [NSString stringWithFormat:@"%d", d], fontBar);
        }
        [hdr appendString:@"static const yami_glyph_t *const gl_digits[10] = {&gl_dig_0,&gl_dig_1,&gl_dig_2,&gl_dig_3,&gl_dig_4,&gl_dig_5,&gl_dig_6,&gl_dig_7,&gl_dig_8,&gl_dig_9};\n"];
        emit_text_glyph(hdr, @"gl_pct",  @"%", fontBar);
        emit_text_glyph(hdr, @"gl_coin_unit", @"币", fontBar);
        emit_text_glyph(hdr, @"gl_diet_lbl",  @"饮食", fontBar);
        emit_text_glyph(hdr, @"gl_mood_lbl",  @"开心", fontBar);

        NSArray *idlePills = @[
            @"扑闪着小翅膀在屋里巡游探险",
            @"软绵绵飘在阳光地毯上打盹",
            @"甩着蓬松大尾巴欢快溜达中",
            @"头顶小橘子慢悠悠散步中",
            @"背着小喷射包在屋里滑行玩耍",
            @"穿着背带裤迈着小短腿巡逻",
            @"头顶闪电天线充能巡游中",
            @"带着极光星辉优雅踱步中",
            @"蹦蹦跳跳嗅着窗外花香呢"
        ];
        for (int i = 0; i < 9; ++i) {
            emit_text_glyph(hdr, [NSString stringWithFormat:@"gl_pill_idle_%d", i], idlePills[i], fontPill);
        }
        [hdr appendString:@"static const yami_glyph_t *const gl_pills_idle[9] = {"];
        for (int i = 0; i < 9; ++i) [hdr appendFormat:@"&gl_pill_idle_%d%@", i, i < 8 ? @"," : @""];
        [hdr appendString:@"};\n"];

        emit_text_glyph(hdr, @"gl_pill_eating",    @"跑向食盆大口享用美食，开心跳起来！", fontPill);
        emit_text_glyph(hdr, @"gl_pill_hungry",    @"肚子咕咕叫，委屈巴巴蹲在地上等你…", fontPill);
        emit_text_glyph(hdr, @"gl_pill_runaway",   @"7天没被照顾，留下一封小信离家出走了…", fontPill);
        emit_text_glyph(hdr, @"gl_pill_lowcoin",   @"宠粮币不足，完成作业或听写可赚20币！", fontPill);
        emit_text_glyph(hdr, @"gl_pill_lowrecall", @"召回需100币(5项任务)，快去写作业攒币吧！", fontPill);

        NSArray *foodBtns = @[
            @[@"能量星果 10币", @"星云大餐 20币", @"曲奇 15币"],
            @[@"鲜脆小食 10币", @"豪华大餐 20币", @"曲奇 15币"],
            @[@"充能果冻 10币", @"超能大餐 20币", @"曲奇 15币"]
        ];
        for (int sp = 0; sp < 3; ++sp) {
            for (int sl = 0; sl < 3; ++sl) {
                emit_text_glyph(hdr, [NSString stringWithFormat:@"gl_fbtn_%d_%d", sp, sl], foodBtns[sp][sl], fontBtn);
            }
        }
        [hdr appendString:@"static const yami_glyph_t *const gl_food_btns[3][3] = {\n"];
        for (int sp = 0; sp < 3; ++sp) {
            [hdr appendFormat:@"    {&gl_fbtn_%d_0, &gl_fbtn_%d_1, &gl_fbtn_%d_2},\n", sp, sp, sp];
        }
        [hdr appendString:@"};\n"];
        emit_text_glyph(hdr, @"gl_btn_recall", @"花 100 币召回宠物 (5项学习任务) | 按右键接回家", fontBtn);

        [hdr writeToFile:outHeader atomically:YES encoding:NSUTF8StringEncoding error:nil];
        printf("Compiled 9 animated 4x4 Hatch-Pet atlases + firmware header (%lu bytes)\n",
               (unsigned long)[hdr length]);
    }
    return 0;
}
'''


def main() -> int:
    WEB_PETS_DIR.mkdir(parents=True, exist_ok=True)
    objc_file = Path("/tmp/compile_hatch_pets.m")
    bin_file = Path("/tmp/compile_hatch_pets_bin")
    objc_file.write_text(OBJC_SOURCE, encoding="utf-8")

    subprocess.run(
        [
            "clang",
            "-O2",
            "-fobjc-arc",
            "-framework",
            "Foundation",
            "-framework",
            "CoreGraphics",
            "-framework",
            "CoreText",
            "-framework",
            "ImageIO",
            str(objc_file),
            "-o",
            str(bin_file),
        ],
        check=True,
    )

    subprocess.run(
        [
            str(bin_file),
            str(ARTIFACT_DIR),
            str(WEB_PETS_DIR),
            str(WEB_ANIME_DIR),
            str(FW_FLUENT_DIR),
            str(FONT_BOLD),
            str(OUT_HEADER),
        ],
        check=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
