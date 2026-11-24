//! Drawing into pixels: the marks (annotations), a coordinate grid and labels are burnt into the PNG the pad
//! exports for the vision model, so the model sees what the user sees (mark 3, x=400 ...).
//! A tiny 5x7 bitmap font keeps this free of font files.

use crate::model::Ann;
use image::{Rgba, RgbaImage};

/// 5x7 glyphs, one byte per row (bit 4 = leftmost column).
fn glyph(c: char) -> [u8; 7] {
    match c.to_ascii_uppercase() {
        '0' => [0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E],
        '1' => [0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E],
        '2' => [0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F],
        '3' => [0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E],
        '4' => [0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02],
        '5' => [0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E],
        '6' => [0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E],
        '7' => [0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08],
        '8' => [0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E],
        '9' => [0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C],
        'A' => [0x0E, 0x11, 0x11, 0x11, 0x1F, 0x11, 0x11],
        'B' => [0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E],
        'C' => [0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E],
        'D' => [0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C],
        'E' => [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F],
        'F' => [0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10],
        'G' => [0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F],
        'H' => [0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11],
        'I' => [0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E],
        'J' => [0x07, 0x02, 0x02, 0x02, 0x02, 0x12, 0x0C],
        'K' => [0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11],
        'L' => [0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F],
        'M' => [0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11],
        'N' => [0x11, 0x11, 0x19, 0x15, 0x13, 0x11, 0x11],
        'O' => [0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
        'P' => [0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10],
        'Q' => [0x0E, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0D],
        'R' => [0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11],
        'S' => [0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E],
        'T' => [0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
        'U' => [0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E],
        'V' => [0x11, 0x11, 0x11, 0x11, 0x11, 0x0A, 0x04],
        'W' => [0x11, 0x11, 0x11, 0x15, 0x15, 0x15, 0x0A],
        'X' => [0x11, 0x11, 0x0A, 0x04, 0x0A, 0x11, 0x11],
        'Y' => [0x11, 0x11, 0x11, 0x0A, 0x04, 0x04, 0x04],
        'Z' => [0x1F, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1F],
        '#' => [0x0A, 0x0A, 0x1F, 0x0A, 0x1F, 0x0A, 0x0A],
        '-' => [0x00, 0x00, 0x00, 0x1F, 0x00, 0x00, 0x00],
        '.' => [0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C],
        ',' => [0x00, 0x00, 0x00, 0x00, 0x0C, 0x04, 0x08],
        ':' => [0x00, 0x0C, 0x0C, 0x00, 0x0C, 0x0C, 0x00],
        '?' => [0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04],
        '!' => [0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04],
        '/' => [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x00],
        '(' => [0x02, 0x04, 0x08, 0x08, 0x08, 0x04, 0x02],
        ')' => [0x08, 0x04, 0x02, 0x02, 0x02, 0x04, 0x08],
        '+' => [0x00, 0x04, 0x04, 0x1F, 0x04, 0x04, 0x00],
        '=' => [0x00, 0x00, 0x1F, 0x00, 0x1F, 0x00, 0x00],
        '\'' => [0x04, 0x04, 0x08, 0x00, 0x00, 0x00, 0x00],
        ' ' => [0; 7],
        _ => [0x1F, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1F],
    }
}

fn blend(img: &mut RgbaImage, x: i64, y: i64, c: [u8; 4]) {
    if x < 0 || y < 0 || x >= img.width() as i64 || y >= img.height() as i64 {
        return;
    }
    let p = img.get_pixel_mut(x as u32, y as u32);
    let a = c[3] as u32;
    for i in 0..3 {
        p.0[i] = ((c[i] as u32 * a + p.0[i] as u32 * (255 - a)) / 255) as u8;
    }
    p.0[3] = p.0[3].max(c[3]);
}

fn dot(img: &mut RgbaImage, cx: f32, cy: f32, r: f32, c: [u8; 4]) {
    let r = r.max(0.5);
    let (x0, x1) = ((cx - r).floor() as i64, (cx + r).ceil() as i64);
    let (y0, y1) = ((cy - r).floor() as i64, (cy + r).ceil() as i64);
    for y in y0..=y1 {
        for x in x0..=x1 {
            let (dx, dy) = (x as f32 + 0.5 - cx, y as f32 + 0.5 - cy);
            if dx * dx + dy * dy <= r * r {
                blend(img, x, y, c);
            }
        }
    }
}

pub fn line(img: &mut RgbaImage, a: [f32; 2], b: [f32; 2], width: f32, c: [u8; 4]) {
    let len = ((b[0] - a[0]).powi(2) + (b[1] - a[1]).powi(2)).sqrt();
    let steps = (len * 2.0).ceil().max(1.0) as usize;
    for i in 0..=steps {
        let t = i as f32 / steps as f32;
        dot(img, a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, width / 2.0, c);
    }
}

fn polyline(img: &mut RgbaImage, pts: &[[f32; 2]], width: f32, c: [u8; 4], closed: bool) {
    for w in pts.windows(2) {
        line(img, w[0], w[1], width, c);
    }
    if closed && pts.len() > 2 {
        line(img, pts[pts.len() - 1], pts[0], width, c);
    }
}

pub fn ellipse_points(a: [f32; 2], b: [f32; 2], n: usize) -> Vec<[f32; 2]> {
    let (cx, cy) = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0);
    let (rx, ry) = (((b[0] - a[0]) / 2.0).abs(), ((b[1] - a[1]) / 2.0).abs());
    (0..n)
        .map(|i| {
            let t = i as f32 / n as f32 * std::f32::consts::TAU;
            [cx + rx * t.cos(), cy + ry * t.sin()]
        })
        .collect()
}

/// The two short strokes of an arrow head at `to`, pointing away from `from`.
pub fn arrow_head(from: [f32; 2], to: [f32; 2], size: f32) -> [[f32; 2]; 2] {
    let (dx, dy) = (to[0] - from[0], to[1] - from[1]);
    let ang = dy.atan2(dx);
    let spread = 0.45f32;
    [
        [to[0] - size * (ang - spread).cos(), to[1] - size * (ang - spread).sin()],
        [to[0] - size * (ang + spread).cos(), to[1] - size * (ang + spread).sin()],
    ]
}

/// Text with a dark plate behind it; `scale` = pixels per font dot. Returns the drawn width.
pub fn text(img: &mut RgbaImage, x: f32, y: f32, s: &str, scale: u32, c: [u8; 4]) -> f32 {
    let scale = scale.max(1) as i64;
    let chars: Vec<char> = s.chars().take(60).collect();
    let w = chars.len() as i64 * 6 * scale + scale;
    let (x0, y0) = (x.round() as i64, y.round() as i64);
    // dark text gets a light plate, light text a dark one
    let lum = 0.299 * c[0] as f32 + 0.587 * c[1] as f32 + 0.114 * c[2] as f32;
    let back = if lum < 110.0 { [255, 255, 255, 200] } else { [0, 0, 0, 170] };
    for yy in y0 - scale..y0 + 8 * scale {
        for xx in x0 - scale..x0 + w {
            blend(img, xx, yy, back);
        }
    }
    for (i, ch) in chars.iter().enumerate() {
        let g = glyph(*ch);
        for (row, bits) in g.iter().enumerate() {
            for col in 0..5 {
                if bits & (0x10 >> col) != 0 {
                    for sy in 0..scale {
                        for sx in 0..scale {
                            blend(img, x0 + (i as i64 * 6 + col) * scale + sx, y0 + row as i64 * scale + sy, c);
                        }
                    }
                }
            }
        }
    }
    w as f32
}

/// Burn one mark into `img`; `map` turns image coordinates into the coordinates of `img` (crop + scale).
pub fn draw_ann(img: &mut RgbaImage, a: &Ann, map: &dyn Fn([f32; 2]) -> [f32; 2], scale: f32) {
    let pts: Vec<[f32; 2]> = a.pts.iter().map(|p| map(*p)).collect();
    if pts.is_empty() {
        return;
    }
    let w = (a.width * scale.max(0.35)).max(2.0);
    let c = a.color;
    match a.shape.as_str() {
        "rect" if pts.len() >= 2 => {
            let (p, q) = (pts[0], pts[1]);
            polyline(img, &[[p[0], p[1]], [q[0], p[1]], [q[0], q[1]], [p[0], q[1]]], w, c, true);
        }
        "ellipse" if pts.len() >= 2 => polyline(img, &ellipse_points(pts[0], pts[1], 72), w, c, true),
        "arrow" if pts.len() >= 2 => {
            line(img, pts[0], pts[1], w, c);
            let h = arrow_head(pts[0], pts[1], 10.0 + 3.0 * w);
            line(img, pts[1], h[0], w, c);
            line(img, pts[1], h[1], w, c);
        }
        "line" if pts.len() >= 2 => line(img, pts[0], pts[1], w, c),
        "pen" => polyline(img, &pts, w, c, false),
        "text" => {
            let s = a.text.clone().unwrap_or_default();
            text(img, pts[0][0], pts[0][1], &s, 2, c);
        }
        _ => {
            // point (and anything degenerate): a ring with a cross
            let p = pts[0];
            polyline(img, &ellipse_points([p[0] - 9.0, p[1] - 9.0], [p[0] + 9.0, p[1] + 9.0], 32), 2.5, c, true);
            line(img, [p[0] - 4.0, p[1]], [p[0] + 4.0, p[1]], 2.0, c);
            line(img, [p[0], p[1] - 4.0], [p[0], p[1] + 4.0], 2.0, c);
        }
    }
    if a.shape != "text" {
        // the mark's number, so "mark 3" means the same thing to the user, the agent and the model
        let p = pts[0];
        let label = match &a.text {
            Some(t) if !t.is_empty() => format!("{} {}", a.id, t),
            _ => a.id.to_string(),
        };
        text(img, p[0] + 6.0, p[1] - 22.0, &label, 2, [255, 255, 255, 255]);
    }
}

/// Grid lines every `step` source pixels, labelled with source coordinates along the top and left edges.
pub fn draw_grid(img: &mut RgbaImage, step: f32, x0: f32, y0: f32, scale: f32) {
    if step <= 0.0 {
        return;
    }
    let (w, h) = (img.width() as f32, img.height() as f32);
    let c = [0, 255, 255, 110];
    let mut gx = (x0 / step).ceil() * step;
    while (gx - x0) * scale < w {
        let x = (gx - x0) * scale;
        line(img, [x, 0.0], [x, h], 1.0, c);
        text(img, x + 2.0, 2.0, &format!("{}", gx as i64), 1, [0, 255, 255, 255]);
        gx += step;
    }
    let mut gy = (y0 / step).ceil() * step;
    while (gy - y0) * scale < h {
        let y = (gy - y0) * scale;
        line(img, [0.0, y], [w, y], 1.0, c);
        text(img, 2.0, y + 2.0, &format!("{}", gy as i64), 1, [0, 255, 255, 255]);
        gy += step;
    }
}

pub fn px(img: &RgbaImage, x: u32, y: u32) -> [u8; 4] {
    let Rgba(p) = *img.get_pixel(x, y);
    p
}
