//! The pad window: toolbar (open, paste, tools, colour, zoom, ask) on top, the image in focus in the middle,
//! the strip of every image ever loaded at the bottom.
//!
//! Mouse / touch: pinch or ⌘/ctrl+scroll or a mouse wheel zooms at the pointer; two-finger scroll pans;
//! right/middle drag pans with any tool; right-click touches a pixel with any tool, alt+click too.
//! Keys: H T C R E A P M X tools · G grid layer · F fit · 1 = 100 % · +/- zoom · ←/→ previous/next image ·
//!       Esc clear selection and picks · ⌘Z undo mark · ⌘O open · ⌘V paste · Enter in "Ask" sends the question.
//! Grid layer (G): disp's map of the image — a dot per perceived component (e5 elements, b2 text blocks) where it was
//! found, link lines for their relations. Click dots (or drag a selection over them) to pick them: the picks go with
//! your next question, so disp knows what you mean.

use crate::model::{Ann, Layer, Model, Tool};
use crate::raster;
use crate::server::Hub;
use eframe::egui::{
    self, pos2, vec2, Align2, Color32, Event, FontId, Key, MouseWheelUnit, PointerButton, Pos2, Rect, Sense, Shape,
    Stroke, StrokeKind, Vec2, ViewportCommand,
};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

const THUMB: f32 = 92.0;
const SLOT: f32 = 108.0;
const STRIP_H: f32 = 128.0;
const SWATCHES: [[u8; 4]; 8] = [
    [255, 64, 64, 255],
    [255, 196, 0, 255],
    [64, 220, 96, 255],
    [0, 220, 230, 255],
    [64, 140, 255, 255],
    [230, 80, 200, 255],
    [255, 255, 255, 255],
    [0, 0, 0, 255],
];

fn c32(c: [u8; 4]) -> Color32 {
    Color32::from_rgba_unmultiplied(c[0], c[1], c[2], c[3])
}

#[derive(Clone, Copy)]
struct View {
    rect: Rect,
    zoom: f32,
    center: [f32; 2],
}

impl View {
    fn screen(&self, p: [f32; 2]) -> Pos2 {
        let c = self.rect.center();
        pos2(c.x + (p[0] - self.center[0]) * self.zoom, c.y + (p[1] - self.center[1]) * self.zoom)
    }
    fn img(&self, s: Pos2) -> [f32; 2] {
        let c = self.rect.center();
        [self.center[0] + (s.x - c.x) / self.zoom, self.center[1] + (s.y - c.y) / self.zoom]
    }
}

pub struct PadApp {
    model: Arc<Mutex<Model>>,
    hub: Arc<Hub>,
    tex: Option<(String, egui::TextureHandle)>,
    thumbs: HashMap<String, egui::TextureHandle>,
    ask: String,
    label: String,
    drag_pts: Vec<[f32; 2]>,
    dragging: bool,
    hover: Option<(u32, u32, [u8; 4])>,
    last_focus: Option<usize>,
    status: String,
    status_until: f64,
    events: Vec<Value>,
    /// the user changed the view (zoom / pan): told to the agent once it settles (for the trace)
    view_dirty: Option<f64>,
    /// the grid dot under the pointer
    hover_node: Option<String>,
    /// strip thumbnails, made from the image files in threads (kept in memory only)
    thumb_tx: std::sync::mpsc::Sender<(String, Option<egui::ColorImage>)>,
    thumb_rx: std::sync::mpsc::Receiver<(String, Option<egui::ColorImage>)>,
    thumb_busy: std::collections::HashSet<String>,
    thumb_missing: std::collections::HashSet<String>,
}

impl PadApp {
    pub fn new(model: Arc<Mutex<Model>>, hub: Arc<Hub>) -> PadApp {
        let (thumb_tx, thumb_rx) = std::sync::mpsc::channel();
        PadApp {
            thumb_tx,
            thumb_rx,
            thumb_busy: Default::default(),
            thumb_missing: Default::default(),
            model,
            hub,
            tex: None,
            thumbs: HashMap::new(),
            ask: String::new(),
            label: String::new(),
            drag_pts: vec![],
            dragging: false,
            hover: None,
            last_focus: None,
            status: String::new(),
            status_until: 0.0,
            events: vec![],
            view_dirty: None,
            hover_node: None,
        }
    }

    fn note(&mut self, s: impl Into<String>) {
        self.status = s.into();
        self.status_until = crate::model::now() + 6.0;
    }

    fn flush_events(&mut self) {
        for ev in self.events.drain(..) {
            self.hub.broadcast(ev);
        }
    }

    fn loaded(&mut self, r: Result<(usize, bool), String>, what: &str) {
        match r {
            Ok((i, new)) => {
                let info = self.model.lock().unwrap().info(i);
                self.note(format!("{} #{} {}", if new { "loaded" } else { "already known:" }, info["n"], info["name"].as_str().unwrap_or("")));
                self.events.push(json!({"ev": "loaded", "image": info, "new": new, "by": "pad"}));
            }
            Err(e) => self.note(format!("{}: {}", what, e)),
        }
    }

    fn open_dialog(&mut self) {
        let files = rfd::FileDialog::new()
            .set_title("Open images in the disp pad")
            .add_filter("images", &["png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "ico"])
            .pick_files();
        for f in files.unwrap_or_default() {
            let r = self.model.lock().unwrap().load_path(&f.to_string_lossy(), "file");
            self.loaded(r, "open");
        }
    }

    fn paste(&mut self) {
        let r = self.model.lock().unwrap().paste();
        self.loaded(r, "paste");
    }

    fn focus(&mut self, i: usize) {
        let mut m = self.model.lock().unwrap();
        if i < m.imgs.len() && m.focus != Some(i) {
            m.set_focus(i);
            let info = m.info(i);
            drop(m);
            self.events.push(json!({"ev": "focus", "image": info}));
        }
    }

    fn send_ask(&mut self) {
        let mut text = self.ask.trim().to_string();
        let (n, sel, clients, refs) = {
            let m = self.model.lock().unwrap();
            (m.focus.map(|i| m.imgs[i].n), m.sel, m.clients, m.picks.clone())
        };
        if text.is_empty() {
            if refs.is_empty() {
                return;
            }
            text = "what are these?".into(); // picks alone ask about them
        }
        if clients == 0 {
            self.note("no agent connected: start disp in Vim (disp1), then ask again");
            return;
        }
        self.events.push(json!({"ev": "ask", "text": text, "n": n, "region": sel, "refs": refs}));
        self.note(if refs.is_empty() { format!("asked disp: {}", text) } else { format!("asked disp about {}: {}", refs.join(" "), text) });
        self.ask.clear();
        self.model.lock().unwrap().picks.clear(); // they went with the question
    }

    fn toggle_grid(&mut self) {
        let mut m = self.model.lock().unwrap();
        m.grid = !m.grid;
        let ev = json!({"ev": "grid", "on": m.grid, "n": m.focus.map(|i| m.imgs[i].n)});
        drop(m);
        self.events.push(ev);
    }

    fn clear_picks(&mut self) {
        let mut m = self.model.lock().unwrap();
        if m.picks.is_empty() {
            return;
        }
        m.picks.clear();
        let n = m.focus.map(|i| m.imgs[i].n);
        drop(m);
        self.events.push(json!({"ev": "pick", "n": n, "id": null, "on": false, "ids": []}));
    }

    fn undo(&mut self) {
        let mut m = self.model.lock().unwrap();
        let Some(i) = m.focus else { return };
        let last = m.imgs[i].anns.iter().rev().find(|a| a.by != "agent").or(m.imgs[i].anns.last()).map(|a| a.id);
        if let Some(id) = last {
            m.clear_anns(i, Some(vec![id]));
            let n = m.imgs[i].n;
            drop(m);
            self.events.push(json!({"ev": "unmark", "n": n, "ids": [id]}));
        }
    }

    /// Dropped files, paste, keyboard shortcuts.
    fn input(&mut self, ctx: &egui::Context) {
        let dropped = ctx.input(|i| i.raw.dropped_files.clone());
        for f in dropped {
            let path = f.path().to_string_lossy().to_string();
            let r = if !path.is_empty() {
                self.model.lock().unwrap().load_path(&path, "drop")
            } else {
                match f.bytes() {
                    Ok(b) => self.model.lock().unwrap().load_bytes(b, "dropped", "drop", "", None),
                    Err(e) => Err(e),
                }
            };
            self.loaded(r, "drop");
        }
        let typing = ctx.egui_wants_keyboard_input();
        let pasted = ctx.input(|i| i.events.iter().any(|e| matches!(e, Event::Paste(_) | Event::PasteImage(_))));
        if pasted && !typing {
            self.paste();
        }
        if typing {
            return;
        }
        let (keys, cmd) = ctx.input(|i| {
            let keys: Vec<Key> = i
                .events
                .iter()
                .filter_map(|e| match e {
                    Event::Key { key, pressed: true, .. } => Some(*key),
                    _ => None,
                })
                .collect();
            (keys, i.modifiers.command)
        });
        for k in keys {
            if cmd {
                match k {
                    Key::O => self.open_dialog(),
                    Key::Z => self.undo(),
                    _ => {}
                }
                continue;
            }
            let tool = match k {
                Key::H => Some(Tool::Hand),
                Key::T => Some(Tool::Touch),
                Key::C => Some(Tool::Select),
                Key::R => Some(Tool::Rect),
                Key::E => Some(Tool::Ellipse),
                Key::A => Some(Tool::Arrow),
                Key::P => Some(Tool::Pen),
                Key::M => Some(Tool::Point),
                Key::X => Some(Tool::Text),
                _ => None,
            };
            if let Some(t) = tool {
                self.model.lock().unwrap().tool = t;
                continue;
            }
            if k == Key::G {
                self.toggle_grid();
                continue;
            }
            if k == Key::Escape {
                self.clear_picks();
            }
            let mut m = self.model.lock().unwrap();
            match k {
                Key::F => m.fit = true,
                Key::Num1 | Key::Num0 => {
                    m.fit = false;
                    m.zoom = 1.0;
                }
                Key::Plus | Key::Equals => {
                    m.fit = false;
                    m.zoom = (m.zoom * 1.25).min(64.0);
                }
                Key::Minus => {
                    m.fit = false;
                    m.zoom = (m.zoom / 1.25).max(0.01);
                }
                Key::Escape => {
                    m.sel = None;
                    self.drag_pts.clear();
                    self.dragging = false;
                }
                Key::ArrowLeft | Key::ArrowRight => {
                    let n = m.imgs.len();
                    if n > 0 {
                        let cur = m.focus.unwrap_or(0);
                        let next = if k == Key::ArrowLeft { (cur + n - 1) % n } else { (cur + 1) % n };
                        drop(m);
                        self.focus(next);
                    }
                }
                _ => {}
            }
        }
    }

    fn toolbar(&mut self, ui: &mut egui::Ui) {
        ui.add_space(3.0);
        ui.horizontal_wrapped(|ui| {
            if ui.button("Open…").on_hover_text("open image files (⌘O); drop files on the window too").clicked() {
                self.open_dialog();
            }
            if ui.button("Paste").on_hover_text("the image on the clipboard (⌘V)").clicked() {
                self.paste();
            }
            ui.separator();
            let cur = self.model.lock().unwrap().tool;
            for t in Tool::ALL {
                let tip = match t {
                    Tool::Hand => "pan: drag the image; double-click: fit / 100 %",
                    Tool::Touch => "touch: click a pixel, the agent gets its position and colour",
                    Tool::Select => "select a region (crop it, zoom to it, ask about it)",
                    Tool::Rect => "draw a rectangle mark",
                    Tool::Ellipse => "draw an ellipse mark",
                    Tool::Arrow => "draw an arrow mark",
                    Tool::Pen => "draw freehand",
                    Tool::Point => "click to pin a numbered point (label: the text box)",
                    Tool::Text => "click to place the text of the text box",
                };
                if ui.selectable_label(cur == t, format!("{} {}", t.key(), t.name())).on_hover_text(tip).clicked() {
                    self.model.lock().unwrap().tool = t;
                }
            }
            ui.separator();
            let (grid_on, nodes) = {
                let m = self.model.lock().unwrap();
                (m.grid, m.focus.and_then(|i| m.layers.get(&m.imgs[i].sha)).map(|l| l.nodes.len()).unwrap_or(0))
            };
            let tip = if nodes > 0 {
                format!("grid layer: the {} components disp perceived in this image (dots, ids e5 / b2) and their \
                         relations (lines); click dots to pick them for your next question (G)", nodes)
            } else {
                "grid layer: disp has no map of this image yet (it maps new images in the background) (G)".to_string()
            };
            let label = if nodes > 0 { format!("G grid ·{}", nodes) } else { "G grid".to_string() };
            if ui.selectable_label(grid_on, label).on_hover_text(tip).clicked() {
                self.toggle_grid();
            }
            ui.separator();
            {
                let mut m = self.model.lock().unwrap();
                for c in SWATCHES {
                    let (r, resp) = ui.allocate_exact_size(vec2(16.0, 16.0), Sense::click());
                    ui.painter().rect_filled(r.shrink(1.0), 3.0, c32(c));
                    if m.color == c {
                        ui.painter().rect_stroke(r, 3.0, Stroke::new(2.0, ui.visuals().strong_text_color()), StrokeKind::Outside);
                    }
                    if resp.clicked() {
                        m.color = c;
                    }
                }
                ui.add(egui::DragValue::new(&mut m.width).range(1.0..=16.0).speed(0.1).suffix(" px"))
                    .on_hover_text("line width of new marks");
            }
            ui.add(egui::TextEdit::singleline(&mut self.label).desired_width(90.0).hint_text("label"))
                .on_hover_text("text for the text tool, label for points and marks");
            if ui.button("Undo").on_hover_text("remove the last mark (⌘Z)").clicked() {
                self.undo();
            }
            if ui.button("Clear").on_hover_text("remove every mark on this image").clicked() {
                let mut m = self.model.lock().unwrap();
                if let Some(i) = m.focus {
                    m.clear_anns(i, None);
                    let n = m.imgs[i].n;
                    drop(m);
                    self.events.push(json!({"ev": "unmark", "n": n}));
                }
            }
            ui.separator();
            let mut crop = false;
            {
                let mut m = self.model.lock().unwrap();
                if ui.button("−").clicked() {
                    m.fit = false;
                    m.zoom = (m.zoom / 1.25).max(0.01);
                }
                ui.label(format!("{:.0}%", m.zoom * 100.0));
                if ui.button("+").clicked() {
                    m.fit = false;
                    m.zoom = (m.zoom * 1.25).min(64.0);
                }
                if ui.selectable_label(m.fit, "Fit").clicked() {
                    m.fit = true;
                }
                if ui.button("1:1").clicked() {
                    m.fit = false;
                    m.zoom = 1.0;
                }
                if let Some(s) = m.sel {
                    ui.separator();
                    ui.label(format!("▭ {}×{} at {},{}", s[2], s[3], s[0], s[1]));
                    if ui.button("Crop").on_hover_text("the selection becomes a new image").clicked() {
                        crop = true;
                    }
                    if ui.button("Zoom").on_hover_text("zoom to the selection").clicked() {
                        m.zoom_to(s);
                    }
                    if ui.button("×").on_hover_text("clear the selection (Esc)").clicked() {
                        m.sel = None;
                    }
                }
            }
            if crop {
                let r = {
                    let mut m = self.model.lock().unwrap();
                    match (m.focus, m.sel) {
                        (Some(i), Some(s)) => Some(m.crop(i, s)),
                        _ => None,
                    }
                };
                if let Some(r) = r {
                    self.loaded(r, "crop");
                }
            }
        });
        ui.horizontal(|ui| {
            ui.label("Ask disp");
            let picks = self.model.lock().unwrap().picks.clone();
            if !picks.is_empty() {
                let shown = if picks.len() > 8 { format!("{} +{}", picks[..8].join(" "), picks.len() - 8) } else { picks.join(" ") };
                ui.label(egui::RichText::new(format!("⦿ {}", shown)).color(Color32::from_rgb(255, 196, 0)).strong())
                    .on_hover_text("the components you picked on the grid: they go with your question (Esc clears)");
                if ui.small_button("×").on_hover_text("clear the picks (Esc)").clicked() {
                    self.clear_picks();
                }
            }
            let w = (ui.available_width() - 330.0).max(160.0);
            let r = ui.add(
                egui::TextEdit::singleline(&mut self.ask)
                    .desired_width(w)
                    .hint_text("a question or a request about this image — Enter sends it to the disp agent in Vim"),
            );
            if r.lost_focus() && ui.input(|i| i.key_pressed(Key::Enter)) {
                self.send_ask();
                r.request_focus();
            }
            let now = crate::model::now();
            let m = self.model.lock().unwrap();
            let pix = self
                .hover
                .map(|(x, y, p)| format!("({}, {}) #{:02x}{:02x}{:02x} a{}", x, y, p[0], p[1], p[2], p[3]))
                .unwrap_or_default();
            let link = if m.clients > 0 { format!("● agent ×{}", m.clients) } else { "○ no agent".to_string() };
            let status = if now < self.status_until { self.status.clone() } else { String::new() };
            drop(m);
            ui.label(egui::RichText::new(link).small()).on_hover_text("clients on the pad's control socket");
            ui.monospace(pix);
            ui.label(egui::RichText::new(status).small().weak());
        });
        ui.add_space(2.0);
    }

    /// The strip's thumbnail of an image, made from its file in a thread (nothing is stored): None until ready.
    fn thumb(&mut self, ctx: &egui::Context, sha: &str, file: std::path::PathBuf) -> Option<egui::TextureHandle> {
        if let Some(t) = self.thumbs.get(sha) {
            return Some(t.clone());
        }
        if self.thumb_busy.contains(sha) || self.thumb_missing.contains(sha) {
            return None;
        }
        self.thumb_busy.insert(sha.to_string());
        let (tx, ctx2, key) = (self.thumb_tx.clone(), ctx.clone(), sha.to_string());
        std::thread::spawn(move || {
            let ci = image::open(&file).ok().map(|im| {
                let t = im.thumbnail(160, 160).to_rgba8();
                egui::ColorImage::from_rgba_unmultiplied([t.width() as usize, t.height() as usize], t.as_raw())
            });
            let _ = tx.send((key, ci));
            ctx2.request_repaint();
        });
        None
    }

    fn take_thumbs(&mut self, ctx: &egui::Context) {
        while let Ok((sha, ci)) = self.thumb_rx.try_recv() {
            self.thumb_busy.remove(&sha);
            match ci {
                Some(ci) => {
                    if self.thumbs.len() > 400 {
                        self.thumbs.clear();
                    }
                    let t = ctx.load_texture(format!("thumb-{}", sha), ci, egui::TextureOptions::LINEAR);
                    self.thumbs.insert(sha, t);
                }
                None => {
                    self.thumb_missing.insert(sha);
                }
            }
        }
    }

    fn strip(&mut self, ui: &mut egui::Ui) {
        let ctx = ui.ctx().clone();
        let (count, focus) = {
            let m = self.model.lock().unwrap();
            (m.imgs.len(), m.focus)
        };
        let scroll_to = if focus != self.last_focus { focus } else { None };
        self.last_focus = focus;
        let mut clicked = None;
        egui::ScrollArea::horizontal().id_salt("strip").auto_shrink([false, false]).show_viewport(ui, |ui, vp| {
            let (rect, _) = ui.allocate_exact_size(vec2(count as f32 * SLOT + 8.0, STRIP_H - 14.0), Sense::hover());
            let slot = |i: usize| Rect::from_min_size(pos2(rect.min.x + 4.0 + i as f32 * SLOT, rect.min.y + 4.0), vec2(SLOT - 8.0, THUMB + 18.0));
            let first = (vp.min.x / SLOT).floor().max(0.0) as usize;
            let last = ((vp.max.x / SLOT).ceil() as usize + 1).min(count);
            for i in first..last {
                let r = slot(i);
                let (sha, dir, n, name, w, h, source, marks) = {
                    let m = self.model.lock().unwrap();
                    let im = &m.imgs[i];
                    (im.sha.clone(), m.file_of(i), im.n, im.name.clone(), im.w, im.h, im.source.clone(), im.anns.len())
                };
                let resp = ui.interact(r, ui.scope_id().with(("thumb", i)), Sense::click());
                let p = ui.painter();
                let on = focus == Some(i);
                p.rect_filled(r, 4.0, if on { Color32::from_rgb(40, 70, 60) } else if resp.hovered() { Color32::from_gray(52) } else { Color32::from_gray(36) });
                if on {
                    p.rect_stroke(r, 4.0, Stroke::new(2.0, Color32::from_rgb(216, 243, 220)), StrokeKind::Inside);
                }
                let tr = Rect::from_min_size(r.min + vec2((r.width() - THUMB) / 2.0, 3.0), vec2(THUMB, THUMB));
                if let Some(t) = self.thumb(&ctx, &sha, dir) {
                    let s = t.size_vec2();
                    let k = (THUMB / s.x).min(THUMB / s.y);
                    let ir = Rect::from_center_size(tr.center(), s * k);
                    p.image(t.id(), ir, Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)), Color32::WHITE);
                } else if self.thumb_missing.contains(&sha) {
                    p.text(tr.center(), Align2::CENTER_CENTER, "file gone", FontId::proportional(12.0), Color32::LIGHT_RED);
                } else {
                    p.text(tr.center(), Align2::CENTER_CENTER, "…", FontId::proportional(20.0), Color32::GRAY);
                }
                let label = format!("{}#{} {}", &sha[..4], n, name);
                let lr = Rect::from_min_size(pos2(r.min.x + 4.0, r.max.y - 15.0), vec2(r.width() - 8.0, 14.0));
                let painter = p.with_clip_rect(lr);
                painter.text(lr.left_center(), Align2::LEFT_CENTER, label, FontId::proportional(11.0),
                    if on { Color32::WHITE } else { Color32::from_gray(190) });
                if marks > 0 {
                    p.text(tr.right_top() + vec2(-2.0, 2.0), Align2::RIGHT_TOP, format!("✏{}", marks), FontId::proportional(11.0), Color32::from_rgb(255, 196, 0));
                }
                let resp = resp.on_hover_text(format!("{}#{} {}\n{}×{} · {}{}", &sha[..4], n, name, w, h, source, if marks > 0 { format!(" · {} marks", marks) } else { String::new() }));
                if resp.clicked() {
                    clicked = Some(i);
                }
            }
            if let Some(f) = scroll_to {
                ui.scroll_to_rect(slot(f), Some(egui::Align::Center));
            }
        });
        if let Some(i) = clicked {
            self.focus(i);
        }
    }

    fn canvas(&mut self, ui: &mut egui::Ui) {
        let ctx = ui.ctx().clone();
        let (rect, resp) = ui.allocate_exact_size(ui.available_size(), Sense::click_and_drag());
        let painter = ui.painter_at(rect);
        painter.rect_filled(rect, 0.0, Color32::from_gray(22));
        let mut m = self.model.lock().unwrap();
        m.view_size = [rect.width().max(1.0), rect.height().max(1.0)];
        let Some(fi) = m.focus else {
            painter.text(rect.center(), Align2::CENTER_CENTER,
                "Open (⌘O), paste (⌘V) or drop an image here\n— or from Vim: dispo on an image path, ===p to paste",
                FontId::proportional(18.0), Color32::from_gray(150));
            self.hover = None;
            return;
        };
        let (w, h, n) = (m.imgs[fi].w as f32, m.imgs[fi].h as f32, m.imgs[fi].n);
        let sha = m.imgs[fi].sha.clone();
        if self.tex.as_ref().map(|(s, _)| s != &sha).unwrap_or(true) {
            match m.pixels(fi) {
                Ok(px) => {
                    let max = ctx.input(|i| i.max_texture_side) as u32;
                    let ci = if px.width() > max || px.height() > max {
                        let s = max as f32 / px.width().max(px.height()) as f32;
                        let r = image::imageops::resize(px.as_ref(), ((px.width() as f32 * s) as u32).max(1),
                            ((px.height() as f32 * s) as u32).max(1), image::imageops::FilterType::Triangle);
                        egui::ColorImage::from_rgba_unmultiplied([r.width() as usize, r.height() as usize], r.as_raw())
                    } else {
                        egui::ColorImage::from_rgba_unmultiplied([px.width() as usize, px.height() as usize], px.as_raw())
                    };
                    let opts = egui::TextureOptions {
                        magnification: egui::TextureFilter::Nearest,
                        mipmap_mode: Some(egui::TextureFilter::Linear),
                        ..egui::TextureOptions::LINEAR
                    };
                    self.tex = Some((sha.clone(), ctx.load_texture(format!("img-{}", sha), ci, opts)));
                    ctx.send_viewport_cmd(ViewportCommand::Title(format!("DISPAD · {} {}", m.imgs[fi].key(), m.imgs[fi].name)));
                }
                Err(e) => {
                    painter.text(rect.center(), Align2::CENTER_CENTER, e, FontId::proportional(16.0), Color32::LIGHT_RED);
                    return;
                }
            }
        }
        if m.fit {
            m.fit_view();
        }

        // ---- zoom & pan
        if resp.hovered() {
            let (zd, wheel) = ctx.input(|i| {
                let mut wheel = (0.0f32, Vec2::ZERO);
                for e in &i.events {
                    if let Event::MouseWheel { unit, delta, modifiers, .. } = e {
                        if modifiers.command || modifiers.ctrl {
                            continue; // egui turns these into zoom_delta
                        }
                        match unit {
                            MouseWheelUnit::Point => wheel.1 += *delta, // trackpad: pan
                            _ => wheel.0 += delta.y,                    // mouse wheel: zoom
                        }
                    }
                }
                (i.zoom_delta(), wheel)
            });
            if let Some(hp) = resp.hover_pos() {
                let mut factor = zd;
                if wheel.0 != 0.0 {
                    factor *= 1.15f32.powf(wheel.0.clamp(-6.0, 6.0));
                }
                if factor != 1.0 {
                    let v = View { rect, zoom: m.zoom, center: m.center };
                    let p = v.img(hp);
                    let nz = (m.zoom * factor).clamp(0.01, 64.0);
                    m.zoom = nz;
                    m.center = [p[0] - (hp.x - rect.center().x) / nz, p[1] - (hp.y - rect.center().y) / nz];
                    m.fit = false;
                }
            }
            if wheel.1 != Vec2::ZERO {
                let z = m.zoom;
                m.center[0] -= wheel.1.x / z;
                m.center[1] -= wheel.1.y / z;
                m.fit = false;
            }
        }
        let tool = m.tool;
        let space = ctx.input(|i| i.key_down(Key::Space)) && !ctx.egui_wants_keyboard_input();
        let pan = resp.dragged_by(PointerButton::Middle)
            || resp.dragged_by(PointerButton::Secondary)
            || ((tool == Tool::Hand || space) && resp.dragged_by(PointerButton::Primary));
        if pan {
            let d = resp.drag_delta();
            let z = m.zoom;
            m.center[0] -= d.x / z;
            m.center[1] -= d.y / z;
            m.fit = false;
        }
        if tool == Tool::Hand && resp.double_clicked() {
            if m.fit || (m.zoom - 1.0).abs() > 0.01 {
                if let Some(hp) = resp.interact_pointer_pos() {
                    let p = View { rect, zoom: m.zoom, center: m.center }.img(hp);
                    m.fit = false;
                    m.zoom = 1.0;
                    m.center = [p[0] - (hp.x - rect.center().x), p[1] - (hp.y - rect.center().y)];
                }
            } else {
                m.fit = true;
                m.fit_view();
            }
        }
        let view = View { rect, zoom: m.zoom, center: m.center };
        if let Some(t) = self.view_dirty {
            if crate::model::now() - t > 0.8 {
                self.view_dirty = None;
                self.events.push(json!({"ev": "view", "n": n, "zoom": m.zoom, "visible": m.visible()}));
            } else {
                ctx.request_repaint_after(std::time::Duration::from_millis(300));
            }
        }
        let inside = |p: [f32; 2]| p[0] >= 0.0 && p[1] >= 0.0 && p[0] < w && p[1] < h;
        let clamp = |p: [f32; 2]| [p[0].clamp(0.0, w), p[1].clamp(0.0, h)];
        let alt = ctx.input(|i| i.modifiers.alt);

        // ---- the grid layer: disp's perceived components (dots) and their links; a click near a dot picks it
        let layer: Option<Layer> = if m.grid { m.layers.get(&m.imgs[fi].sha).cloned() } else { None };
        let node_at = |sp: Pos2, max: f32| -> Option<String> {
            let l = layer.as_ref()?;
            let mut best: Option<(f32, &str)> = None;
            for nd in &l.nodes {
                let d = view.screen([nd.x, nd.y]).distance(sp);
                if d <= max && best.map(|b| d < b.0).unwrap_or(true) {
                    best = Some((d, nd.id.as_str()));
                }
            }
            best.map(|b| b.1.to_string())
        };
        self.hover_node = resp.hover_pos().and_then(|hp| node_at(hp, 10.0));
        let mut grid_click = false;
        if layer.is_some() && resp.clicked_by(PointerButton::Primary) && !alt {
            if let Some(id) = resp.interact_pointer_pos().and_then(|sp| node_at(sp, 12.0)) {
                grid_click = true;
                let on = match m.picks.iter().position(|p| *p == id) {
                    Some(k) => {
                        m.picks.remove(k);
                        false
                    }
                    None => {
                        m.picks.push(id.clone());
                        true
                    }
                };
                self.events.push(json!({"ev": "pick", "n": n, "id": id, "on": on, "ids": m.picks}));
            }
        }

        // ---- touch: a pixel goes to the agent
        let touch_at = if !grid_click && resp.clicked_by(PointerButton::Primary) && (tool == Tool::Touch || alt) {
            resp.interact_pointer_pos().map(|p| (p, "primary"))
        } else if resp.clicked_by(PointerButton::Secondary) {
            resp.interact_pointer_pos().map(|p| (p, "secondary"))
        } else {
            None
        };
        if let Some((sp, button)) = touch_at {
            let p = view.img(sp);
            if inside(p) {
                let (x, y) = (p[0].floor(), p[1].floor());
                if let Ok(px) = m.pixel(fi, x, y) {
                    let hex = format!("#{:02x}{:02x}{:02x}", px[0], px[1], px[2]);
                    self.events.push(json!({"ev": "click", "n": n, "x": x as u32, "y": y as u32, "rgba": px, "hex": hex, "button": button}));
                    self.status = format!("touched #{} ({}, {}) {}", n, x, y, hex);
                    self.status_until = crate::model::now() + 6.0;
                }
            }
        } else if !grid_click && resp.clicked_by(PointerButton::Primary) && matches!(tool, Tool::Point | Tool::Text) {
            if let Some(sp) = resp.interact_pointer_pos() {
                let p = clamp(view.img(sp));
                let label = self.label.trim().to_string();
                if tool == Tool::Text && label.is_empty() {
                    self.status = "type the text in the label box first".into();
                    self.status_until = crate::model::now() + 6.0;
                } else {
                    let ann = Ann { id: 0, shape: tool.name().into(), pts: vec![[p[0].round(), p[1].round()]], color: m.color,
                        width: m.width, text: (!label.is_empty()).then_some(label), by: "user".into() };
                    m.add_ann(fi, ann);
                    let mark = json!(m.imgs[fi].anns.last());
                    self.events.push(json!({"ev": "mark", "n": n, "mark": mark}));
                }
            }
        }

        // ---- drags that draw: selection and shapes
        let draws = matches!(tool, Tool::Select | Tool::Rect | Tool::Ellipse | Tool::Arrow | Tool::Pen) && !space && !alt;
        if draws && resp.drag_started_by(PointerButton::Primary) {
            let origin = ctx.input(|i| i.pointer.press_origin()).or(resp.interact_pointer_pos());
            if let Some(o) = origin {
                let p = clamp(view.img(o));
                self.drag_pts = vec![p, p];
                self.dragging = true;
            }
        }
        if self.dragging && resp.dragged_by(PointerButton::Primary) {
            if let Some(sp) = resp.interact_pointer_pos() {
                let p = clamp(view.img(sp));
                if tool == Tool::Pen {
                    let last = *self.drag_pts.last().unwrap();
                    if ((p[0] - last[0]).powi(2) + (p[1] - last[1]).powi(2)).sqrt() * view.zoom > 2.0 {
                        self.drag_pts.push(p);
                    }
                } else if self.drag_pts.len() >= 2 {
                    self.drag_pts[1] = p;
                }
            }
        }
        if self.dragging && resp.drag_stopped_by(PointerButton::Primary) {
            self.dragging = false;
            let pts = std::mem::take(&mut self.drag_pts);
            let (a, b) = (pts[0], *pts.last().unwrap());
            let big = ((b[0] - a[0]).abs().max((b[1] - a[1]).abs())) * view.zoom > 3.0;
            if tool == Tool::Select && big {
                let r = crate::model::clamp_rect([a[0], a[1], b[0] - a[0], b[1] - a[1]], w as u32, h as u32);
                let r = [r[0] as f32, r[1] as f32, r[2] as f32, r[3] as f32];
                m.sel = Some(r);
                self.events.push(json!({"ev": "select", "n": n, "region": r}));
                if let Some(l) = &layer {
                    // on the grid, a selection also picks the dots inside it
                    let mut added = vec![];
                    for nd in &l.nodes {
                        let inside = nd.x >= r[0] && nd.x <= r[0] + r[2] && nd.y >= r[1] && nd.y <= r[1] + r[3];
                        if inside && !m.picks.contains(&nd.id) {
                            m.picks.push(nd.id.clone());
                            added.push(nd.id.clone());
                        }
                    }
                    if !added.is_empty() {
                        self.events.push(json!({"ev": "pick", "n": n, "added": added, "on": true, "ids": m.picks}));
                    }
                }
            } else if tool != Tool::Select && (big || (tool == Tool::Pen && pts.len() > 2)) {
                let pts: Vec<[f32; 2]> = if tool == Tool::Pen { pts } else { vec![a, b] };
                let pts = pts.iter().map(|p| [p[0].round(), p[1].round()]).collect();
                let label = self.label.trim().to_string();
                let ann = Ann { id: 0, shape: tool.name().into(), pts, color: m.color, width: m.width,
                    text: (!label.is_empty()).then_some(label), by: "user".into() };
                m.add_ann(fi, ann);
                let mark = json!(m.imgs[fi].anns.last());
                self.events.push(json!({"ev": "mark", "n": n, "mark": mark}));
            }
        }

        // ---- hover: the pixel under the pointer
        self.hover = resp.hover_pos().map(|hp| view.img(hp)).filter(|p| inside(*p)).and_then(|p| {
            let (x, y) = (p[0].floor(), p[1].floor());
            m.pixel(fi, x, y).ok().map(|px| (x as u32, y as u32, px))
        });

        // ---- paint
        if let Some((_, tex)) = &self.tex {
            let ir = Rect::from_min_max(view.screen([0.0, 0.0]), view.screen([w, h]));
            painter.rect_filled(ir, 0.0, Color32::from_gray(40));
            painter.image(tex.id(), ir, Rect::from_min_max(pos2(0.0, 0.0), pos2(1.0, 1.0)), Color32::WHITE);
            painter.rect_stroke(ir, 0.0, Stroke::new(1.0, Color32::from_gray(70)), StrokeKind::Outside);
            if view.zoom >= 12.0 {
                let a = view.img(rect.min);
                let b = view.img(rect.max);
                let (x0, x1) = (a[0].floor().max(0.0) as i64, b[0].ceil().min(w) as i64);
                let (y0, y1) = (a[1].floor().max(0.0) as i64, b[1].ceil().min(h) as i64);
                if (x1 - x0) < 400 && (y1 - y0) < 400 {
                    let st = Stroke::new(1.0, Color32::from_white_alpha(26));
                    for x in x0..=x1 {
                        painter.line_segment([view.screen([x as f32, y0 as f32]), view.screen([x as f32, y1 as f32])], st);
                    }
                    for y in y0..=y1 {
                        painter.line_segment([view.screen([x0 as f32, y as f32]), view.screen([x1 as f32, y as f32])], st);
                    }
                }
            }
        }
        for a in &m.imgs[fi].anns {
            draw_ann(&painter, a, &view);
        }
        if let Some(l) = &layer {
            draw_layer(&painter, l, &view, &m.picks, self.hover_node.as_deref());
        }
        if self.dragging && self.drag_pts.len() >= 2 {
            if tool == Tool::Select {
                let (a, b) = (self.drag_pts[0], self.drag_pts[1]);
                let r = Rect::from_two_pos(view.screen(a), view.screen(b));
                painter.rect(r, 0.0, Color32::from_rgba_unmultiplied(0, 220, 230, 30), Stroke::new(1.5, Color32::from_rgb(0, 220, 230)), StrokeKind::Middle);
                painter.text(r.left_top() - vec2(0.0, 2.0), Align2::LEFT_BOTTOM,
                    format!("{}×{}", (b[0] - a[0]).abs().round(), (b[1] - a[1]).abs().round()), FontId::monospace(12.0), Color32::from_rgb(0, 220, 230));
            } else {
                let pts = if tool == Tool::Pen { self.drag_pts.clone() } else { vec![self.drag_pts[0], self.drag_pts[1]] };
                let ann = Ann { id: 0, shape: tool.name().into(), pts, color: m.color, width: m.width, text: None, by: "user".into() };
                draw_ann(&painter, &ann, &view);
            }
        }
        if let Some(s) = m.sel {
            let r = Rect::from_min_max(view.screen([s[0], s[1]]), view.screen([s[0] + s[2], s[1] + s[3]]));
            painter.rect(r, 0.0, Color32::from_rgba_unmultiplied(0, 220, 230, 18), Stroke::new(1.5, Color32::from_rgb(0, 220, 230)), StrokeKind::Middle);
            plate(&painter, r.left_top() + vec2(0.0, -18.0), &format!("▭ {}×{} at {},{}", s[2], s[3], s[0], s[1]), Color32::from_rgb(0, 220, 230), 12.0);
        }
        let info = format!("{} {} · {}×{} · {:.0}% · {}", m.imgs[fi].key(), m.imgs[fi].name, w, h, m.zoom * 100.0, tool.name());
        plate(&painter, rect.left_bottom() + vec2(8.0, -24.0), &info, Color32::from_gray(220), 13.0);
        let now = crate::model::now();
        if let Some((text, until)) = m.banner.clone() {
            if now < until {
                let galley = painter.layout_no_wrap(text, FontId::proportional(17.0), Color32::BLACK);
                let br = Rect::from_center_size(pos2(rect.center().x, rect.min.y + 26.0), galley.size() + vec2(24.0, 12.0));
                painter.rect_filled(br, 6.0, Color32::from_rgb(255, 196, 0));
                painter.galley(br.min + vec2(12.0, 6.0), galley, Color32::BLACK);
                ctx.request_repaint_after(std::time::Duration::from_secs_f64((until - now).max(0.05)));
            } else {
                m.banner = None;
            }
        }
    }
}

fn node_color(ty: &str) -> Color32 {
    match ty {
        "text" => Color32::from_rgb(240, 240, 240),
        "button" => Color32::from_rgb(254, 128, 25),
        "box" => Color32::from_rgb(0, 200, 220),
        "icon" => Color32::from_rgb(230, 80, 200),
        "shape" => Color32::from_rgb(120, 220, 90),
        "figure" => Color32::from_rgb(100, 150, 255),
        "region" => Color32::from_rgb(160, 160, 160),
        "pattern" => Color32::from_rgb(210, 190, 90),
        "block" => Color32::from_rgb(190, 120, 255),
        _ => Color32::WHITE,
    }
}

/// The grid layer: links under the dots (meaning: amber; text block: violet; position: faint white; containment:
/// only around the dots you point at or picked), then a dot per component with its id; the hovered one says what it is.
fn draw_layer(p: &egui::Painter, l: &Layer, v: &View, picks: &[String], hover: Option<&str>) {
    let pos: HashMap<&str, Pos2> = l.nodes.iter().map(|nd| (nd.id.as_str(), v.screen([nd.x, nd.y]))).collect();
    let hot = |id: &str| picks.iter().any(|p| p == id) || hover == Some(id);
    for k in &l.links {
        let (Some(a), Some(b)) = (pos.get(k.a.as_str()), pos.get(k.b.as_str())) else { continue };
        let lit = hot(&k.a) || hot(&k.b);
        let (col, wd) = match k.kind.as_str() {
            "sem" => (Color32::from_rgba_unmultiplied(255, 196, 0, if lit { 255 } else { 150 }), if lit { 2.2 } else { 1.4 }),
            "block" => (Color32::from_rgba_unmultiplied(190, 120, 255, if lit { 240 } else { 120 }), if lit { 2.0 } else { 1.0 }),
            "tree" => {
                if !lit {
                    continue;
                }
                (Color32::from_rgba_unmultiplied(0, 200, 220, 210), 1.3)
            }
            _ => (Color32::from_rgba_unmultiplied(255, 255, 255, if lit { 210 } else { 40 }), if lit { 1.6 } else { 1.0 }),
        };
        p.line_segment([*a, *b], Stroke::new(wd, col));
        if lit && !k.rel.is_empty() {
            plate(p, pos2((a.x + b.x) / 2.0 + 3.0, (a.y + b.y) / 2.0 - 9.0), &k.rel, col, 10.0);
        }
    }
    for nd in &l.nodes {
        if let (true, Some(b)) = (hot(&nd.id), nd.bbox) {
            let r = Rect::from_min_max(v.screen([b[0], b[1]]), v.screen([b[0] + b[2], b[1] + b[3]]));
            p.rect_stroke(r, 2.0, Stroke::new(1.2, node_color(&nd.ty)), StrokeKind::Middle);
        }
    }
    for nd in &l.nodes {
        let c = pos[nd.id.as_str()];
        let col = node_color(&nd.ty);
        let r = if hot(&nd.id) { 5.5 } else { 4.0 };
        if nd.ty == "block" {
            let pts = vec![c + vec2(0.0, -r - 1.0), c + vec2(r + 1.0, 0.0), c + vec2(0.0, r + 1.0), c + vec2(-r - 1.0, 0.0)];
            p.add(Shape::convex_polygon(pts, col, Stroke::new(1.0, Color32::from_black_alpha(220))));
        } else {
            p.circle(c, r, col, Stroke::new(1.0, Color32::from_black_alpha(220)));
        }
        if picks.iter().any(|x| *x == nd.id) {
            p.circle_stroke(c, 9.5, Stroke::new(2.0, Color32::from_rgb(255, 196, 0)));
        }
        plate(p, c + vec2(6.0, -16.0), &nd.id, col, 10.0);
    }
    if let Some(nd) = hover.and_then(|h| l.nodes.iter().find(|nd| nd.id == h)) {
        let b = nd.bbox.map(|b| format!(" {}[{},{} {}×{}]", if nd.exact { "" } else { "≈" }, b[0] as i64, b[1] as i64, b[2] as i64, b[3] as i64))
            .unwrap_or_default();
        let label: String = nd.label.chars().take(60).collect();
        plate(p, pos[nd.id.as_str()] + vec2(12.0, 6.0), &format!("{} {} \"{}\"{} · click to pick", nd.id, nd.ty, label, b),
            Color32::WHITE, 12.0);
    }
}

/// Text on a dark plate.
fn plate(p: &egui::Painter, at: Pos2, text: &str, color: Color32, size: f32) {
    let galley = p.layout_no_wrap(text.to_string(), FontId::proportional(size), color);
    let r = Rect::from_min_size(at, galley.size()).expand(3.0);
    let lum = 0.299 * color.r() as f32 + 0.587 * color.g() as f32 + 0.114 * color.b() as f32;
    p.rect_filled(r, 3.0, if lum < 110.0 { Color32::from_white_alpha(200) } else { Color32::from_black_alpha(170) });
    p.galley(at, galley, color);
}

fn draw_ann(p: &egui::Painter, a: &Ann, v: &View) {
    let c = c32(a.color);
    let st = Stroke::new(a.width.max(1.0), c);
    let pts: Vec<Pos2> = a.pts.iter().map(|q| v.screen(*q)).collect();
    if pts.is_empty() {
        return;
    }
    match a.shape.as_str() {
        "rect" if pts.len() >= 2 => {
            p.rect_stroke(Rect::from_two_pos(pts[0], pts[1]), 0.0, st, StrokeKind::Middle);
        }
        "ellipse" if pts.len() >= 2 => {
            let e: Vec<Pos2> = raster::ellipse_points([pts[0].x, pts[0].y], [pts[1].x, pts[1].y], 72)
                .into_iter().map(|q| pos2(q[0], q[1])).collect();
            p.add(Shape::closed_line(e, st));
        }
        "arrow" if pts.len() >= 2 => {
            p.line_segment([pts[0], pts[1]], st);
            let hd = raster::arrow_head([pts[0].x, pts[0].y], [pts[1].x, pts[1].y], 10.0 + 2.5 * a.width);
            p.line_segment([pts[1], pos2(hd[0][0], hd[0][1])], st);
            p.line_segment([pts[1], pos2(hd[1][0], hd[1][1])], st);
        }
        "line" if pts.len() >= 2 => {
            p.line_segment([pts[0], pts[1]], st);
        }
        "pen" => {
            p.add(Shape::line(pts.clone(), st));
        }
        "text" => {
            plate(p, pts[0], a.text.as_deref().unwrap_or(""), c, 13.0 + a.width * 1.5);
        }
        _ => {
            let q = pts[0];
            p.circle_stroke(q, 9.0, Stroke::new(2.5, c));
            p.line_segment([q - vec2(4.0, 0.0), q + vec2(4.0, 0.0)], Stroke::new(2.0, c));
            p.line_segment([q - vec2(0.0, 4.0), q + vec2(0.0, 4.0)], Stroke::new(2.0, c));
        }
    }
    if a.shape != "text" && a.id > 0 {
        let label = match &a.text {
            Some(t) if !t.is_empty() => format!("{} {}", a.id, t),
            _ => a.id.to_string(),
        };
        plate(p, pts[0] + vec2(7.0, -21.0), &label, Color32::WHITE, 12.0);
    }
}

impl eframe::App for PadApp {
    fn logic(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let win = ctx.input(|i| {
            let v = i.viewport();
            json!({"visible": v.visible(), "focused": v.focused, "occluded": v.occluded, "minimized": v.minimized})
        });
        // files that hold other bytes than when they were loaded: new content for the clients (disp maps it)
        let changed: Vec<Value> = {
            let mut m = self.model.lock().unwrap();
            let c = std::mem::take(&mut m.changed);
            c.iter().map(|&i| m.info(i)).collect()
        };
        for info in changed {
            self.hub.broadcast(json!({"ev": "loaded", "image": info, "new": true, "by": "pad", "changed": true}));
        }
        let (raise, quit, shot) = {
            let mut m = self.model.lock().unwrap();
            m.window = win;
            m.passes.0 += 1;
            let r = (m.raise, m.quit, m.shot.take());
            m.raise = false;
            m.quit = false;
            r
        };
        if let Some(tx) = shot {
            ctx.send_viewport_cmd(ViewportCommand::Screenshot(egui::ScreenshotCallback::new(move |img| {
                let _ = tx.send(img);
            })));
        }
        if raise {
            ctx.send_viewport_cmd(ViewportCommand::Minimized(false));
            ctx.send_viewport_cmd(ViewportCommand::Visible(true));
            ctx.send_viewport_cmd(ViewportCommand::Focus);
        }
        if quit {
            ctx.send_viewport_cmd(ViewportCommand::Close); // on_exit says bye
        }
    }

    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        self.take_thumbs(&ctx);
        // what the user does to the view in this pass (requests from the agent land between passes)
        let before = {
            let mut m = self.model.lock().unwrap();
            m.passes.1 += 1;
            (m.focus, m.fit, m.zoom, m.center)
        };
        self.input(&ctx);
        egui::Panel::top("tools").show(ui, |ui| self.toolbar(ui));
        egui::Panel::bottom("strip").exact_size(STRIP_H).show(ui, |ui| self.strip(ui));
        egui::CentralPanel::no_frame().show(ui, |ui| self.canvas(ui));
        {
            let m = self.model.lock().unwrap();
            let same_image = before.0 == m.focus;
            if same_image && (before.1 != m.fit || (!m.fit && (before.2 != m.zoom || before.3 != m.center))) {
                self.view_dirty = Some(crate::model::now());
                ctx.request_repaint_after(std::time::Duration::from_millis(300));
            }
        }
        self.flush_events();
    }

    fn on_exit(&mut self) {
        self.hub.broadcast(json!({"ev": "bye"}));
    }
}
