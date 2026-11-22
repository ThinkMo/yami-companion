#!/usr/bin/env python3
"""
Compile 9 distinct breed anime scenes + Runaway empty-nest scene + 3D food/emotion icons
+ TrueType anti-aliased Chinese glyph atlas into `yami_pet_sprites.h` using macOS CoreGraphics & CoreText.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WEB_ANIME_DIR = ROOT / "apps" / "guardian" / "assets" / "anime"
FW_UI_DIR = ROOT / "firmware" / "esp-mosaico-vibe" / "projects" / "yami" / "components" / "app_hello" / "ui"
FW_ANIME_DIR = FW_UI_DIR / "assets" / "anime"
FW_FLUENT_DIR = FW_UI_DIR / "assets" / "fluent3d"
FONT_BOLD = FW_UI_DIR / "fonts" / "YamiCN-Bold.ttc"
OUT_HEADER = ROOT / "firmware" / "esp-mosaico-vibe" / "projects" / "yami" / "components" / "app_hello" / "yami_pet_sprites.h"

OBJC_SOURCE = r'''
#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <CoreText/CoreText.h>
#import <ImageIO/ImageIO.h>

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

/* Crop sub-rect (in normalized 0..1 coordinates, top-left origin) from src and draw into w x h context */
static void draw_cropped(CGContextRef ctx, int dst_w, int dst_h, CGImageRef src,
                         double nx, double ny, double nw, double nh) {
    size_t sw = CGImageGetWidth(src);
    size_t sh = CGImageGetHeight(src);
    CGRect cropRect = CGRectMake(nx * sw, ny * sh, nw * sw, nh * sh);
    CGImageRef sub = CGImageCreateWithImageInRect(src, cropRect);
    if (sub) {
        CGContextDrawImage(ctx, CGRectMake(0, 0, dst_w, dst_h), sub);
        CGImageRelease(sub);
    }
}

/* Paint the Runaway Empty Nest scene: crops the starry manor window & antique wooden room
   (top of dog_star_manor, completely above the pets) and places an empty soft rug cushion,
   a parchment farewell letter with red wax seal, and fading paw prints */
static void paint_runaway_empty_nest(CGContextRef ctx, int w, int h, CGImageRef manor_src) {
    /* Top 50% of dog_star_manor has zero animals: starry gothic window, bookshelf, lantern, armchair */
    draw_cropped(ctx, w, h, manor_src, 0.02, 0.04, 0.96, 0.44);
    uint8_t *px = (uint8_t *)CGBitmapContextGetData(ctx);

    for (int y = 0; y < h; ++y) {
        for (int x = 0; x < w; ++x) {
            uint8_t *p = px + (y * w + x) * 4;
            float nx = (float)x / (float)w;
            float ny = (float)y / (float)h;
            float vx = nx - 0.5f, vy = ny - 0.5f;
            float vig = 1.0f - 0.30f * (vx * vx + vy * vy);
            p[0] = (uint8_t)(p[0] * 0.90f * vig);
            p[1] = (uint8_t)(p[1] * 0.90f * vig);
            p[2] = (uint8_t)MIN(255, (int)(p[2] * 0.96f * vig + 6));
        }
    }

    CGContextSaveGState(ctx);
    /* Warm empty woven rug cushion at bottom center */
    CGContextSetRGBFillColor(ctx, 0.24f, 0.18f, 0.14f, 0.72f);
    CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.18f, h * 0.06f, w * 0.64f, h * 0.24f));
    CGContextSetRGBFillColor(ctx, 0.84f, 0.76f, 0.64f, 0.88f);
    CGContextFillEllipseInRect(ctx, CGRectMake(w * 0.22f, h * 0.08f, w * 0.56f, h * 0.20f));

    /* Parchment farewell letter card resting on the empty rug */
    float env_x = w * 0.35f, env_y = h * 0.15f, env_w = w * 0.30f, env_h = h * 0.28f;

    /* Soft drop shadow */
    CGContextSetRGBFillColor(ctx, 0.06f, 0.06f, 0.08f, 0.48f);
    CGPathRef shadowPath = CGPathCreateWithRoundedRect(CGRectMake(env_x + 4, env_y - 5, env_w, env_h), 10, 10, NULL);
    CGContextAddPath(ctx, shadowPath);
    CGContextFillPath(ctx);
    CGPathRelease(shadowPath);

    /* Envelope body */
    CGContextSetRGBFillColor(ctx, 0.98f, 0.94f, 0.86f, 0.98f);
    CGPathRef envPath = CGPathCreateWithRoundedRect(CGRectMake(env_x, env_y, env_w, env_h), 10, 10, NULL);
    CGContextAddPath(ctx, envPath);
    CGContextFillPath(ctx);
    CGContextSetRGBStrokeColor(ctx, 0.72f, 0.56f, 0.40f, 1.0f);
    CGContextSetLineWidth(ctx, 2.5f);
    CGContextAddPath(ctx, envPath);
    CGContextStrokePath(ctx);
    CGPathRelease(envPath);

    /* Envelope flap V line */
    CGContextMoveToPoint(ctx, env_x + 4, env_y + env_h - 4);
    CGContextAddLineToPoint(ctx, env_x + env_w * 0.5f, env_y + env_h * 0.44f);
    CGContextAddLineToPoint(ctx, env_x + env_w - 4, env_y + env_h - 4);
    CGContextStrokePath(ctx);

    /* Red wax seal with inner gold ring */
    float seal_r = env_w * 0.13f;
    float seal_cx = env_x + env_w * 0.5f;
    float seal_cy = env_y + env_h * 0.46f;
    CGContextSetRGBFillColor(ctx, 0.85f, 0.20f, 0.24f, 1.0f);
    CGContextFillEllipseInRect(ctx, CGRectMake(seal_cx - seal_r, seal_cy - seal_r, seal_r * 2, seal_r * 2));
    CGContextSetRGBStrokeColor(ctx, 0.98f, 0.84f, 0.46f, 0.9f);
    CGContextSetLineWidth(ctx, 1.8f);
    CGContextStrokeEllipseInRect(ctx, CGRectMake(seal_cx - seal_r * 0.7f, seal_cy - seal_r * 0.7f, seal_r * 1.4f, seal_r * 1.4f));

    /* Fading paw prints leading away from the empty cushion toward the window */
    float paw_pts[3][2] = {{0.70f, 0.22f}, {0.79f, 0.30f}, {0.88f, 0.38f}};
    for (int i = 0; i < 3; ++i) {
        float px_c = w * paw_pts[i][0], py_c = h * paw_pts[i][1];
        CGContextSetRGBFillColor(ctx, 0.96f, 0.88f, 0.74f, 0.65f - i * 0.14f);
        CGContextFillEllipseInRect(ctx, CGRectMake(px_c - 7, py_c - 5, 14, 10));
        CGContextFillEllipseInRect(ctx, CGRectMake(px_c - 10, py_c + 6, 5, 5));
        CGContextFillEllipseInRect(ctx, CGRectMake(px_c - 2, py_c + 8, 5, 5));
        CGContextFillEllipseInRect(ctx, CGRectMake(px_c + 6, py_c + 6, 5, 5));
    }
    CGContextRestoreGState(ctx);
}

static uint16_t to_rgb565(uint8_t r, uint8_t g, uint8_t b) {
    return (uint16_t)((((uint16_t)r & 0xF8) << 8) | (((uint16_t)g & 0xFC) << 3) | ((uint16_t)b >> 3));
}

static void emit_scene_rgb565(NSMutableString *out, NSString *c_name, CGImageRef img, int w, int h) {
    CGContextRef ctx = create_rgba_ctx(w, h);
    CGContextDrawImage(ctx, CGRectMake(0, 0, w, h), img);
    uint8_t *px = (uint8_t *)CGBitmapContextGetData(ctx);
    [out appendFormat:@"static const uint16_t %@[%d] = {", c_name, w * h];
    for (int i = 0; i < w * h; ++i) {
        uint8_t r = px[i * 4 + 0];
        uint8_t g = px[i * 4 + 1];
        uint8_t b = px[i * 4 + 2];
        [out appendFormat:@"0x%04x%@", to_rgb565(r, g, b), (i + 1 < w * h) ? @"," : @""];
    }
    [out appendString:@"};\n"];
    CGContextRelease(ctx);
}

static void emit_sprite_rgba(NSMutableString *out, NSString *c_name, NSString *path, int w, int h) {
    CGImageRef src = load_image(path);
    CGContextRef ctx = create_rgba_ctx(w, h);
    if (src) {
        CGContextDrawImage(ctx, CGRectMake(0, 0, w, h), src);
        CGImageRelease(src);
    }
    uint8_t *px = (uint8_t *)CGBitmapContextGetData(ctx);
    [out appendFormat:@"static const uint16_t %@_rgb[%d] = {", c_name, w * h];
    for (int i = 0; i < w * h; ++i) {
        uint8_t a = px[i * 4 + 3];
        uint8_t r = px[i * 4 + 0];
        uint8_t g = px[i * 4 + 1];
        uint8_t b = px[i * 4 + 2];
        if (a > 0) {
            r = (uint8_t)MIN(255, (int)r * 255 / (int)a);
            g = (uint8_t)MIN(255, (int)g * 255 / (int)a);
            b = (uint8_t)MIN(255, (int)b * 255 / (int)a);
        }
        [out appendFormat:@"0x%04x%@", to_rgb565(r, g, b), (i + 1 < w * h) ? @"," : @""];
    }
    [out appendString:@"};\n"];
    [out appendFormat:@"static const uint8_t %@_a[%d] = {", c_name, w * h];
    for (int i = 0; i < w * h; ++i) {
        [out appendFormat:@"%u%@", px[i * 4 + 3], (i + 1 < w * h) ? @"," : @""];
    }
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
    for (int i = 0; i < w * h; ++i) {
        [out appendFormat:@"%u%@", px[i * 4 + 3], (i + 1 < w * h) ? @"," : @""];
    }
    [out appendString:@"};\n"];
    [out appendFormat:@"static const yami_glyph_t %@ = {%d, %d, %@_a};\n", c_name, w, h, c_name];
    CGContextRelease(ctx);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 5) return 1;
        NSString *webAnimeDir = [NSString stringWithUTF8String:argv[1]];
        NSString *fwFluentDir = [NSString stringWithUTF8String:argv[2]];
        NSString *fontPath    = [NSString stringWithUTF8String:argv[3]];
        NSString *outHeader   = [NSString stringWithUTF8String:argv[4]];

        CGImageRef img_golden  = load_image([webAnimeDir stringByAppendingPathComponent:@"anime_pet_1_golden_house.png"]);
        CGImageRef img_cats    = load_image([webAnimeDir stringByAppendingPathComponent:@"anime_pet_2_cat_garden.png"]);
        CGImageRef img_beetles = load_image([webAnimeDir stringByAppendingPathComponent:@"anime_pet_3_beetle_terrarium.png"]);
        CGImageRef img_dogs    = load_image([webAnimeDir stringByAppendingPathComponent:@"anime_pet_4_dog_star_manor.png"]);
        CGImageRef img_calico  = load_image([webAnimeDir stringByAppendingPathComponent:@"anime_pet_5_calico_room.png"]);
        CGImageRef img_cattree = load_image([webAnimeDir stringByAppendingPathComponent:@"anime_pet_6_cat_tree.png"]);

        /* 1. Produce 9 distinct breed 660x444 Polaroid stage PNGs + 1 Runaway Empty Nest PNG */
        int stage_w = 660, stage_h = 444;
        struct {
            NSString *filename;
            CGImageRef src;
            double nx, ny, nw, nh;
            int warm_shift; /* 0=none, 1=golden-amber elephas tint */
        } breeds[9] = {
            /* 0: orange */   {@"breed_0_orange.png",   img_cattree, 0.14, 0.34, 0.84, 0.62, 0},
            /* 1: ragdoll */  {@"breed_1_ragdoll.png",  img_cats,    0.00, 0.14, 0.72, 0.74, 0},
            /* 2: calico */   {@"breed_2_calico.png",   img_calico,  0.00, 0.08, 1.00, 0.78, 0},
            /* 3: golden */   {@"breed_3_golden.png",   img_golden,  0.00, 0.12, 1.00, 0.72, 0},
            /* 4: husky */    {@"breed_4_husky.png",    img_dogs,    0.34, 0.24, 0.64, 0.66, 0},
            /* 5: corgi */    {@"breed_5_corgi.png",    img_dogs,    0.04, 0.26, 0.62, 0.64, 0},
            /* 6: kabuto */   {@"breed_6_kabuto.png",   img_beetles, 0.38, 0.18, 0.62, 0.64, 0},
            /* 7: hercules */ {@"breed_7_hercules.png", img_beetles, 0.00, 0.16, 0.66, 0.66, 0},
            /* 8: elephas */  {@"breed_8_elephas.png",  img_beetles, 0.08, 0.14, 0.84, 0.68, 1},
        };

        NSMutableString *hdr = [NSMutableString stringWithCapacity:2 * 1024 * 1024];
        [hdr appendString:@"// Auto-generated Forest Cream Polaroid Pet Scenes (9 Distinct Breeds + Runaway + TrueType Glyph Atlas)\n"];
        [hdr appendString:@"#pragma once\n#include <stdint.h>\n"];
        [hdr appendString:@"#define YAMI_ANIME_W 220\n#define YAMI_ANIME_H 148\n"];
        [hdr appendString:@"typedef struct { uint16_t w, h; const uint16_t *rgb565; const uint8_t *alpha; } yami_sprite3d_t;\n"];
        [hdr appendString:@"typedef struct { uint16_t w, h; const uint8_t *alpha; } yami_glyph_t;\n\n"];

        for (int i = 0; i < 9; ++i) {
            CGContextRef ctx = create_rgba_ctx(stage_w, stage_h);
            draw_cropped(ctx, stage_w, stage_h, breeds[i].src,
                         breeds[i].nx, breeds[i].ny, breeds[i].nw, breeds[i].nh);
            if (breeds[i].warm_shift == 1) {
                /* Golden velvet amber glow for Elephas beetle */
                uint8_t *px = (uint8_t *)CGBitmapContextGetData(ctx);
                for (int k = 0; k < stage_w * stage_h; ++k) {
                    uint8_t *p = px + k * 4;
                    p[0] = (uint8_t)MIN(255, (int)(p[0] * 1.14f + 12));
                    p[1] = (uint8_t)MIN(255, (int)(p[1] * 1.05f + 6));
                    p[2] = (uint8_t)(p[2] * 0.84f);
                }
            }
            CGImageRef outImg = CGBitmapContextCreateImage(ctx);
            save_png(outImg, [webAnimeDir stringByAppendingPathComponent:breeds[i].filename]);
            emit_scene_rgb565(hdr, [NSString stringWithFormat:@"anime_breed_%d_rgb", i], outImg, 220, 148);
            CGImageRelease(outImg);
            CGContextRelease(ctx);
        }

        /* Runaway Empty Nest scene */
        {
            CGContextRef ctx = create_rgba_ctx(stage_w, stage_h);
            paint_runaway_empty_nest(ctx, stage_w, stage_h, img_dogs);
            CGImageRef outImg = CGBitmapContextCreateImage(ctx);
            save_png(outImg, [webAnimeDir stringByAppendingPathComponent:@"scene_runaway_empty_nest.png"]);
            emit_scene_rgb565(hdr, @"anime_scene_runaway_rgb", outImg, 220, 148);
            CGImageRelease(outImg);
            CGContextRelease(ctx);
        }

        [hdr appendString:@"static const uint16_t *const anime_breed_scenes[9] = {\n"];
        for (int i = 0; i < 9; ++i) {
            [hdr appendFormat:@"    anime_breed_%d_rgb,\n", i];
        }
        [hdr appendString:@"};\n\n"];

        /* 2. Emit 3D food & heart sprites (both 38x38 stage size and 20x20 bottom button icon size) */
        emit_sprite_rgba(hdr, @"sp_heart",      [fwFluentDir stringByAppendingPathComponent:@"heart.png"],  36, 36);
        emit_sprite_rgba(hdr, @"sp_bone",       [fwFluentDir stringByAppendingPathComponent:@"bone.png"],   38, 38);
        emit_sprite_rgba(hdr, @"sp_fish",       [fwFluentDir stringByAppendingPathComponent:@"fish.png"],   38, 38);
        emit_sprite_rgba(hdr, @"sp_honey",      [fwFluentDir stringByAppendingPathComponent:@"honey.png"],  38, 38);
        emit_sprite_rgba(hdr, @"sp_cookie",     [fwFluentDir stringByAppendingPathComponent:@"cookie.png"], 38, 38);

        emit_sprite_rgba(hdr, @"ico_bone",      [fwFluentDir stringByAppendingPathComponent:@"bone.png"],   20, 20);
        emit_sprite_rgba(hdr, @"ico_fish",      [fwFluentDir stringByAppendingPathComponent:@"fish.png"],   20, 20);
        emit_sprite_rgba(hdr, @"ico_honey",     [fwFluentDir stringByAppendingPathComponent:@"honey.png"],  20, 20);
        emit_sprite_rgba(hdr, @"ico_cookie",    [fwFluentDir stringByAppendingPathComponent:@"cookie.png"], 20, 20);

        /* 3. Emit TrueType anti-aliased Chinese glyph bitmaps for the 480x480 Polaroid UI */
        CTFontRef fontTitle = load_custom_font(fontPath, 17.0);
        CTFontRef fontPill  = load_custom_font(fontPath, 12.5);
        CTFontRef fontBar   = load_custom_font(fontPath, 13.0);
        CTFontRef fontBtn   = load_custom_font(fontPath, 13.5);

        NSArray *breedTitles = @[
            @"橘猫•小橘", @"布偶•布丁", @"三花•花花",
            @"金毛•暖暖", @"哈士奇•二哈", @"柯基•短腿基",
            @"独角仙•铁角", @"长戟•长戟王", @"毛象•毛象君"
        ];
        for (int i = 0; i < 9; ++i) {
            emit_text_glyph(hdr, [NSString stringWithFormat:@"gl_breed_title_%d", i], breedTitles[i], fontTitle);
        }
        [hdr appendString:@"static const yami_glyph_t *const gl_breed_titles[9] = {"];
        for (int i = 0; i < 9; ++i) [hdr appendFormat:@"&gl_breed_title_%d%@", i, i < 8 ? @"," : @""];
        [hdr appendString:@"};\n\n"];

        /* Habitat Lv.1..4 badges per species (cat=0, dog=1, beetle=2) */
        NSArray *habLabels = @[
            @[@"Lv.1 温馨纸箱窝", @"Lv.2 阳光木屋",   @"Lv.3 阳光庭院",   @"Lv.4 星空庄园"],
            @[@"Lv.1 温暖小狗窝", @"Lv.2 原木小筑",   @"Lv.3 阳光庭院",   @"Lv.4 星空庄园"],
            @[@"Lv.1 生态观察盒", @"Lv.2 朽木饲养箱", @"Lv.3 雨林生态缸", @"Lv.4 星空生态缸"]
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

        /* Digits 0..9, %, 币, 饮食, 开心 */
        for (int d = 0; d <= 9; ++d) {
            emit_text_glyph(hdr, [NSString stringWithFormat:@"gl_dig_%d", d], [NSString stringWithFormat:@"%d", d], fontBar);
        }
        [hdr appendString:@"static const yami_glyph_t *const gl_digits[10] = {&gl_dig_0,&gl_dig_1,&gl_dig_2,&gl_dig_3,&gl_dig_4,&gl_dig_5,&gl_dig_6,&gl_dig_7,&gl_dig_8,&gl_dig_9};\n"];
        emit_text_glyph(hdr, @"gl_pct",  @"%", fontBar);
        emit_text_glyph(hdr, @"gl_coin_unit", @"币", fontBar);
        emit_text_glyph(hdr, @"gl_diet_lbl",  @"饮食", fontBar);
        emit_text_glyph(hdr, @"gl_mood_lbl",  @"开心", fontBar);

        /* Status pills for 9 breeds in HAPPY_IDLE + shared EATING_JOY, HUNGRY_SAD, RUNAWAY, and notice pills */
        NSArray *idlePills = @[
            @"窝在猫爬架上晒太阳呼噜噜",
            @"依偎在地毯上晒太阳呼噜噜",
            @"揣着小爪子在窗台看风景",
            @"摇着尾巴守在食盆旁",
            @"歪着脑袋眼神清澈等你玩",
            @"趴在草坪软垫上摇晃小短腿",
            @"在苔藓木桩上威风抬起Y字角",
            @"在雨林木桩上品尝香蕉果冻",
            @"披着金绒铠甲安静守护小窝"
        ];
        for (int i = 0; i < 9; ++i) {
            emit_text_glyph(hdr, [NSString stringWithFormat:@"gl_pill_idle_%d", i], idlePills[i], fontPill);
        }
        [hdr appendString:@"static const yami_glyph_t *const gl_pills_idle[9] = {"];
        for (int i = 0; i < 9; ++i) [hdr appendFormat:@"&gl_pill_idle_%d%@", i, i < 8 ? @"," : @""];
        [hdr appendString:@"};\n"];

        emit_text_glyph(hdr, @"gl_pill_eating",  @"津津有味享用作夜大餐，幸福冒爱心！", fontPill);
        emit_text_glyph(hdr, @"gl_pill_hungry",  @"肚子咕咕叫，眼巴巴守着空食盆等你…", fontPill);
        emit_text_glyph(hdr, @"gl_pill_runaway", @"7天没被照顾，留下一封小信离家出走了…", fontPill);
        emit_text_glyph(hdr, @"gl_pill_lowcoin", @"宠粮币不足，完成作业或听写可赚20币！", fontPill);
        emit_text_glyph(hdr, @"gl_pill_lowrecall", @"召回需100币(5项任务)，快去写作业攒币吧！", fontPill);

        /* Bottom 3 food capsule labels per species (cat=0, dog=1, beetle=2) */
        NSArray *foodBtns = @[
            @[@"小鱼干 10币", @"猫罐头 20币", @"曲奇 15币"],
            @[@"肉骨头 10币", @"鲜肉餐 20币", @"曲奇 15币"],
            @[@"香蕉冻 10币", @"黑糖冻 20币", @"曲奇 15币"]
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
        printf("Successfully generated 10 anime scenes and wrote %s (%lu bytes)\n",
               [outHeader UTF8String], (unsigned long)[hdr length]);
    }
    return 0;
}
'''


def main() -> int:
    WEB_ANIME_DIR.mkdir(parents=True, exist_ok=True)
    FW_ANIME_DIR.mkdir(parents=True, exist_ok=True)

    objc_file = Path("/tmp/compile_pet_assets.m")
    bin_file = Path("/tmp/compile_pet_assets_bin")
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
            str(WEB_ANIME_DIR),
            str(FW_FLUENT_DIR),
            str(FONT_BOLD),
            str(OUT_HEADER),
        ],
        check=True,
    )

    # Mirror generated breed_0..8.png and scene_runaway_empty_nest.png to firmware ui/assets/anime/
    for png in WEB_ANIME_DIR.glob("*.png"):
        target = FW_ANIME_DIR / png.name
        if png.resolve() != target.resolve():
            shutil.copy2(png, target)

    # Also copy 3D food icons to apps/guardian/assets/fluent3d/ for Web card
    web_fluent = ROOT / "apps" / "guardian" / "assets" / "fluent3d"
    web_fluent.mkdir(parents=True, exist_ok=True)
    for icon in FW_FLUENT_DIR.glob("*.png"):
        shutil.copy2(icon, web_fluent / icon.name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
