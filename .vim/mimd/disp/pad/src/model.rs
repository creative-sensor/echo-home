//! What the pad holds: every image ever loaded, the one in focus, how it is viewed, the selection, the marks.
//! Shared between the window (ui.rs) and the control socket (server.rs) behind one mutex.
//!
//! Images are read where they are: the pad keeps their path, never a copy. The sha256 of the bytes is computed on
//! every load and every read, and names the image's folder in disp's memory (home = ~/echo-home/memory/gddr9b, where
//! disp keeps meta.yaml, map.yaml, notes.yaml, trace.jsonl). The pad writes there only:
//!   <home>/catalog.jsonl                     one line per load: sha, path, name, source, size, mtime, w, h, ext, ts
//!                                            (every image ever loaded, numbered #1, #2 … in the order first loaded;
//!                                            the last line of a sha says where it is now)
//! The strip shows this session's images only (empty at startup; the `clear` op empties it again): an image comes into
//! it when it is loaded or focused, keeping its number for good.
//!   <home>/<d1>/<d2>/<d3d4>/<sha256>/marks.json   the marks drawn on the image
//!   <home>/<d1>/<d2>/<d3d4>/<sha256>/image.png    only for an image with no file of its own (clipboard, crop)
//! Thumbnails are made in memory; what the vision model is shown is cut on demand and sent inline: nothing derived
//! is stored.

use crate::raster;
use image::{imageops::FilterType, DynamicImage, RgbaImage};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub const USER_COLOR: [u8; 4] = [255, 64, 64, 255];
pub const AGENT_COLOR: [u8; 4] = [255, 196, 0, 255];

fn default_color() -> [u8; 4] {
    USER_COLOR
}
fn default_width() -> f32 {
    3.0
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Ann {
    pub id: u32,
    pub shape: String,
    pub pts: Vec<[f32; 2]>,
    #[serde(default = "default_color")]
    pub color: [u8; 4],
    #[serde(default = "default_width")]
    pub width: f32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default)]
    pub by: String,
}

/// An image of the strip. Serialized as its catalog line (the marks live in marks.json).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Img {
    #[serde(skip)]
    pub n: usize,
    pub sha: String,
    pub name: String,
    pub source: String,
    /// where the bytes are read from: the file it was opened from, or <sha>/image.png for a paste / crop
    pub path: String,
    pub w: u32,
    pub h: u32,
    pub ts: f64,
    pub ext: String,
    #[serde(default)]
    pub parent: Option<String>,
    /// size and modification time (s) of the file when last read: a change means the bytes must be hashed again
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub mtime: u64,
    #[serde(skip)]
    pub anns: Vec<Ann>,
    #[serde(skip)]
    pub next_ann: u32,
}

#[derive(Serialize, Deserialize, Default)]
struct Marks {
    #[serde(default)]
    anns: Vec<Ann>,
    #[serde(default)]
    next_ann: u32,
}

/// (size, mtime in seconds) of a file, or None when it is not there.
pub fn stat(path: &Path) -> Option<(u64, u64)> {
    let md = fs::metadata(path).ok()?;
    let mtime = md.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs()).unwrap_or(0);
    Some((md.len(), mtime))
}

/// A perceived component on the grid layer: an element (e5) or a text block (b2) of disp's map, at its position.
/// The layer comes from disp (the map lives in its memory) and is kept here in memory only.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Node {
    pub id: String,
    #[serde(rename = "type", default)]
    pub ty: String,
    #[serde(default)]
    pub label: String,
    pub x: f32,
    pub y: f32,
    #[serde(rename = "box", default)]
    pub bbox: Option<[f32; 4]>,
    #[serde(default)]
    pub exact: bool,
}

/// A relation between two components: kind sem (meaning, from the LLM / disp), geo (position), block (a line of a
/// text block), tree (containment).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Link {
    pub a: String,
    pub b: String,
    #[serde(default)]
    pub rel: String,
    #[serde(default)]
    pub kind: String,
}

#[derive(Clone, Debug, Default)]
pub struct Layer {
    pub nodes: Vec<Node>,
    pub links: Vec<Link>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tool {
    Hand,
    Touch,
    Select,
    Rect,
    Ellipse,
    Arrow,
    Pen,
    Point,
    Text,
}

impl Tool {
    pub const ALL: [Tool; 9] =
        [Tool::Hand, Tool::Touch, Tool::Select, Tool::Rect, Tool::Ellipse, Tool::Arrow, Tool::Pen, Tool::Point, Tool::Text];
    pub fn name(self) -> &'static str {
        match self {
            Tool::Hand => "hand",
            Tool::Touch => "touch",
            Tool::Select => "select",
            Tool::Rect => "rect",
            Tool::Ellipse => "ellipse",
            Tool::Arrow => "arrow",
            Tool::Pen => "pen",
            Tool::Point => "point",
            Tool::Text => "text",
        }
    }
    pub fn from_name(s: &str) -> Option<Tool> {
        let s = s.to_ascii_lowercase();
        let s = match s.as_str() {
            "crop" | "region" | "sel" => "select",
            "pan" | "move" => "hand",
            "pick" | "click" | "pixel" => "touch",
            "circle" => "ellipse",
            "draw" | "free" => "pen",
            "marker" | "mark" => "point",
            other => other,
        };
        Tool::ALL.iter().copied().find(|t| t.name() == s)
    }
    pub fn key(self) -> &'static str {
        match self {
            Tool::Hand => "H",
            Tool::Touch => "T",
            Tool::Select => "C",
            Tool::Rect => "R",
            Tool::Ellipse => "E",
            Tool::Arrow => "A",
            Tool::Pen => "P",
            Tool::Point => "M",
            Tool::Text => "X",
        }
    }
}

pub struct Model {
    pub home: PathBuf,
    pub imgs: Vec<Img>,
    /// this session's images (indexes into imgs), in the order they came into the strip
    pub shown: Vec<usize>,
    /// images the last recall brought up (index -> the R numbers of its memories): tagged on the strip
    pub recalled: HashMap<usize, Vec<u32>>,
    /// a strip position to bring into view (set by a recall), taken by the window
    pub strip_to: Option<usize>,
    by_sha: HashMap<String, usize>,
    pub focus: Option<usize>,
    pixels: HashMap<String, Arc<RgbaImage>>,
    // view (the window keeps these current; requests may set them)
    pub fit: bool,
    pub zoom: f32,
    pub center: [f32; 2],
    pub view_size: [f32; 2],
    pub sel: Option<[f32; 4]>,
    pub tool: Tool,
    pub color: [u8; 4],
    pub width: f32,
    pub banner: Option<(String, f64)>,
    pub raise: bool,
    pub quit: bool,
    pub port: u16,
    pub clients: usize,
    /// grid layers by image sha (pushed by disp), shown while `grid` is on; `picks`: the components the user picked
    /// on the image in focus (sent along with the next question)
    pub layers: HashMap<String, Layer>,
    pub grid: bool,
    pub picks: Vec<String>,
    /// the window as the OS reports it: {"visible","focused","occluded","minimized"} (null = not known yet)
    pub window: Value,
    /// passes run by the window: (logic, ui) — ui stays still while the OS does not let the window draw
    pub passes: (u64, u64),
    /// a screenshot asked on the socket: the window thread sends the command (commands sent from other threads
    /// between two passes are lost) and the pixels come back through this channel
    pub shot: Option<std::sync::mpsc::Sender<Arc<eframe::egui::ColorImage>>>,
    /// images whose file now holds other bytes (found when read again): told to the clients as new content
    pub changed: Vec<usize>,
}

pub fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{:02x}", b)).collect()
}

pub fn sha256(bytes: &[u8]) -> String {
    hex(Sha256::digest(bytes).as_slice())
}

/// Integer region inside a w x h image: [x, y, w, h] clamped, at least 1x1.
pub fn clamp_rect(r: [f32; 4], w: u32, h: u32) -> [u32; 4] {
    let (mut x0, mut y0, mut x1, mut y1) = (r[0], r[1], r[0] + r[2], r[1] + r[3]);
    if x1 < x0 {
        std::mem::swap(&mut x0, &mut x1);
    }
    if y1 < y0 {
        std::mem::swap(&mut y0, &mut y1);
    }
    let x0 = (x0.floor().max(0.0) as u32).min(w.saturating_sub(1));
    let y0 = (y0.floor().max(0.0) as u32).min(h.saturating_sub(1));
    let x1 = (x1.ceil().max(0.0) as u32).min(w).max(x0 + 1);
    let y1 = (y1.ceil().max(0.0) as u32).min(h).max(y0 + 1);
    [x0, y0, x1 - x0, y1 - y0]
}

impl Img {
    /// How the image is named everywhere (pad, Vim panes, the agent): <first 4 hex of its sha256>#<number>.
    pub fn key(&self) -> String {
        format!("{}#{}", &self.sha[..4.min(self.sha.len())], self.n)
    }
}

impl Model {
    pub fn new(home: PathBuf, port: u16) -> Model {
        let mut m = Model {
            home,
            imgs: vec![],
            shown: vec![],
            recalled: HashMap::new(),
            strip_to: None,
            by_sha: HashMap::new(),
            focus: None,
            pixels: HashMap::new(),
            fit: true,
            zoom: 1.0,
            center: [0.0, 0.0],
            view_size: [800.0, 600.0],
            sel: None,
            tool: Tool::Hand,
            color: USER_COLOR,
            width: 3.0,
            banner: None,
            raise: false,
            quit: false,
            port,
            clients: 0,
            layers: HashMap::new(),
            grid: false,
            picks: vec![],
            window: Value::Null,
            passes: (0, 0),
            shot: None,
            changed: vec![],
        };
        let _ = fs::create_dir_all(&m.home);
        m.read_catalog();
        m
    }

    /// The images of earlier sessions, in the order they were first loaded; a later line of the same content
    /// (opened again, maybe from another place) updates where it is.
    fn read_catalog(&mut self) {
        let Ok(text) = fs::read_to_string(self.home.join("catalog.jsonl")) else { return };
        for line in text.lines() {
            let Ok(mut img) = serde_json::from_str::<Img>(line) else { continue };
            if let Some(&i) = self.by_sha.get(&img.sha) {
                let old = &mut self.imgs[i];
                if !img.path.is_empty() {
                    old.path = img.path;
                    old.size = img.size;
                    old.mtime = img.mtime;
                }
                continue;
            }
            img.n = self.imgs.len() + 1;
            self.by_sha.insert(img.sha.clone(), self.imgs.len());
            self.imgs.push(img);
        }
        for i in 0..self.imgs.len() {
            self.load_marks(i);
        }
    }

    fn load_marks(&mut self, i: usize) {
        let f = self.dir_of(&self.imgs[i].sha).join("marks.json");
        let mk: Marks = fs::read_to_string(&f).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
        let img = &mut self.imgs[i];
        img.next_ann = mk.next_ann.max(mk.anns.iter().map(|a| a.id).max().unwrap_or(0) + 1);
        img.anns = mk.anns;
    }

    pub fn dir_of(&self, sha: &str) -> PathBuf {
        let s = sha.as_bytes();
        let part = |a: usize, b: usize| String::from_utf8_lossy(&s[a.min(s.len())..b.min(s.len())]).to_string();
        self.home.join(part(0, 1)).join(part(1, 2)).join(part(2, 4)).join(sha)
    }

    pub fn file_of(&self, i: usize) -> PathBuf {
        expand(&self.imgs[i].path)
    }

    pub fn info(&self, i: usize) -> Value {
        let img = &self.imgs[i];
        let dir = self.dir_of(&img.sha);
        let file = self.file_of(i);
        json!({"n": img.n, "key": img.key(), "sha": img.sha, "name": img.name, "source": img.source, "path": img.path,
               "w": img.w, "h": img.h, "ts": img.ts, "parent": img.parent, "marks": img.anns.len(),
               "file": file.to_string_lossy(), "dir": dir.to_string_lossy(), "missing": !file.is_file(),
               "focus": self.focus == Some(i)})
    }

    /// The marks of an image, in its folder of disp's memory.
    pub fn save_marks(&self, i: usize) {
        let img = &self.imgs[i];
        let dir = self.dir_of(&img.sha);
        let f = dir.join("marks.json");
        if img.anns.is_empty() && !f.exists() {
            return;
        }
        let _ = fs::create_dir_all(&dir);
        if let Ok(t) = serde_json::to_string_pretty(&json!({"anns": img.anns, "next_ann": img.next_ann})) {
            let tmp = dir.join("marks.json.tmp");
            if fs::write(&tmp, t).is_ok() {
                let _ = fs::rename(&tmp, f);
            }
        }
    }

    fn log_load(&self, img: &Img) {
        let mut line = json!(img);
        line["ts"] = json!(now());
        if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(self.home.join("catalog.jsonl")) {
            let _ = writeln!(f, "{}", line);
        }
    }

    /// The file of an image changed on disk since it was read (size or time): its pixels must be read again.
    fn check(&mut self, i: usize) {
        let img = &self.imgs[i];
        if stat(&expand(&img.path)) != Some((img.size, img.mtime)) {
            let sha = img.sha.clone();
            self.pixels.remove(&sha);
        }
    }

    /// Find an image by number (#3, "3"), sha prefix, or name. None = the focused one.
    pub fn resolve(&self, v: Option<&Value>) -> Result<usize, String> {
        let none = || self.focus.ok_or_else(|| "no image in focus (open one first)".to_string());
        let Some(v) = v else { return none() };
        if v.is_null() {
            return none();
        }
        if let Some(n) = v.as_u64() {
            return self.by_n(n as usize);
        }
        let s = v.as_str().unwrap_or("").trim().trim_start_matches('#').to_string();
        if s.is_empty() || s == "focus" || s == "current" {
            return none();
        }
        if let Ok(n) = s.parse::<usize>() {
            return self.by_n(n);
        }
        if let Some((pre, num)) = s.split_once('#') {
            // 3fa2#3: the number, checked against the sha prefix
            let n: usize = num.parse().map_err(|_| format!("no image '{}'", s))?;
            let i = self.by_n(n)?;
            if self.imgs[i].sha.starts_with(&pre.to_ascii_lowercase()) {
                return Ok(i);
            }
            return Err(format!("#{} is {}, not {}", n, self.imgs[i].key(), s));
        }
        let low = s.to_ascii_lowercase();
        if low.len() >= 4 && low.chars().all(|c| c.is_ascii_hexdigit()) {
            let hits: Vec<usize> = (0..self.imgs.len()).filter(|&i| self.imgs[i].sha.starts_with(&low)).collect();
            if hits.len() == 1 {
                return Ok(hits[0]);
            }
        }
        (0..self.imgs.len())
            .rev()
            .find(|&i| self.imgs[i].name.to_ascii_lowercase() == low)
            .ok_or_else(|| format!("no image '{}'", s))
    }

    fn by_n(&self, n: usize) -> Result<usize, String> {
        if n >= 1 && n <= self.imgs.len() {
            Ok(n - 1)
        } else {
            Err(format!("no image #{} (there are {})", n, self.imgs.len()))
        }
    }

    /// Decoded pixels (a few kept in memory), read from the image's file: hashed again, so a file whose bytes
    /// changed becomes that new content (other sha, other memory), told to the clients as a new image.
    pub fn pixels(&mut self, i: usize) -> Result<Arc<RgbaImage>, String> {
        let sha = self.imgs[i].sha.clone();
        if let Some(p) = self.pixels.get(&sha) {
            return Ok(p.clone());
        }
        let file = self.file_of(i);
        let bytes = fs::read(&file).map_err(|e| {
            format!("{} is gone from {} ({}): moved or deleted? open it again from where it is now",
                    self.imgs[i].key(), file.display(), e)
        })?;
        let img = image::load_from_memory(&bytes).map_err(|e| format!("cannot decode {}: {}", file.display(), e))?;
        let now_sha = sha256(&bytes);
        let (size, mtime) = stat(&file).unwrap_or((bytes.len() as u64, 0));
        if now_sha != sha {
            // the same path, other bytes: this entry follows the file, its memory is the new content's
            self.by_sha.remove(&sha);
            self.by_sha.insert(now_sha.clone(), i);
            let e = &mut self.imgs[i];
            e.sha = now_sha.clone();
            e.w = img.width();
            e.h = img.height();
            e.ext = image::guess_format(&bytes).ok().and_then(|f| f.extensions_str().first().copied()).unwrap_or("img").to_string();
            e.size = size;
            e.mtime = mtime;
            self.load_marks(i);
            let snapshot = self.imgs[i].clone();
            self.log_load(&snapshot);
            self.changed.push(i);
        } else if (size, mtime) != (self.imgs[i].size, self.imgs[i].mtime) {
            self.imgs[i].size = size;
            self.imgs[i].mtime = mtime;
        }
        let sha = now_sha;
        let px = Arc::new(img.to_rgba8());
        if self.pixels.len() >= 4 {
            let keep = self.focus.map(|f| self.imgs[f].sha.clone());
            if let Some(k) = self.pixels.keys().find(|k| Some(*k) != keep.as_ref()).cloned() {
                self.pixels.remove(&k);
            }
        }
        self.pixels.insert(sha, px.clone());
        Ok(px)
    }

    /// Load bytes into the memory (new content gets a folder; known content is just focused again).
    /// Returns (index, new?).
    pub fn load_bytes(&mut self, bytes: Vec<u8>, name: &str, source: &str, path: &str, parent: Option<String>)
        -> Result<(usize, bool), String> {
        let sha = sha256(&bytes);
        if let Some(&i) = self.by_sha.get(&sha) {
            // known content: it may come from another place now (the newest place is where it is read from)
            if !path.is_empty() && self.imgs[i].path != path {
                self.imgs[i].path = path.to_string();
                if let Some((size, mtime)) = stat(Path::new(path)) {
                    self.imgs[i].size = size;
                    self.imgs[i].mtime = mtime;
                }
            }
            let img = self.imgs[i].clone();
            self.log_load(&img);
            self.set_focus(i);
            return Ok((i, false));
        }
        let fmt = image::guess_format(&bytes).map_err(|e| format!("not an image: {}", e))?;
        let dynimg = image::load_from_memory_with_format(&bytes, fmt).map_err(|e| format!("cannot decode: {}", e))?;
        let ext = fmt.extensions_str().first().copied().unwrap_or("img").to_string();
        let path = if path.is_empty() {
            // no file of its own (clipboard, crop): kept once, in its folder of disp's memory
            let dir = self.dir_of(&sha);
            fs::create_dir_all(&dir).map_err(|e| format!("cannot create {}: {}", dir.display(), e))?;
            let f = dir.join(format!("image.{}", ext));
            fs::write(&f, &bytes).map_err(|e| format!("cannot keep the image: {}", e))?;
            f.to_string_lossy().to_string()
        } else {
            path.to_string()
        };
        let (size, mtime) = stat(Path::new(&path)).unwrap_or((bytes.len() as u64, 0));
        let img = Img {
            n: self.imgs.len() + 1,
            sha: sha.clone(),
            name: if name.is_empty() { format!("image-{}.{}", &sha[..8], ext) } else { name.to_string() },
            source: source.to_string(),
            path,
            w: dynimg.width(),
            h: dynimg.height(),
            ts: now(),
            ext,
            parent,
            size,
            mtime,
            anns: vec![],
            next_ann: 1,
        };
        self.log_load(&img);
        self.by_sha.insert(sha.clone(), self.imgs.len());
        self.imgs.push(img);
        let i = self.imgs.len() - 1;
        self.load_marks(i); // marks drawn on this content in an earlier life
        self.pixels.insert(sha, Arc::new(dynimg.to_rgba8()));
        self.set_focus(i);
        Ok((i, true))
    }

    pub fn load_path(&mut self, path: &str, source: &str) -> Result<(usize, bool), String> {
        let p = expand(path);
        let bytes = fs::read(&p).map_err(|e| format!("cannot read {}: {}", p.display(), e))?;
        let name = p.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
        let abs = fs::canonicalize(&p).unwrap_or(p.clone());
        self.load_bytes(bytes, &name, source, &abs.to_string_lossy(), None)
    }

    pub fn load_rgba(&mut self, w: u32, h: u32, rgba: Vec<u8>, name: &str, source: &str, parent: Option<String>)
        -> Result<(usize, bool), String> {
        let img = RgbaImage::from_raw(w, h, rgba).ok_or("bad pixel buffer")?;
        let mut png = std::io::Cursor::new(Vec::new());
        DynamicImage::ImageRgba8(img).write_to(&mut png, image::ImageFormat::Png).map_err(|e| e.to_string())?;
        self.load_bytes(png.into_inner(), name, source, "", parent)
    }

    /// The image on the OS clipboard (or an image file path copied as text).
    pub fn paste(&mut self) -> Result<(usize, bool), String> {
        let mut cb = arboard::Clipboard::new().map_err(|e| format!("clipboard: {}", e))?;
        // a file copied in the Finder comes with its icon as an image: the file wins
        if let Ok(files) = cb.get().file_list() {
            let exts = ["png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "ico"];
            let mut last = None;
            for f in files.iter().filter(|f| {
                f.extension().map(|e| exts.contains(&e.to_string_lossy().to_ascii_lowercase().as_str())).unwrap_or(false)
            }) {
                last = Some(self.load_path(&f.to_string_lossy(), "clipboard")?);
            }
            if let Some(r) = last {
                return Ok(r);
            }
        }
        if let Ok(img) = cb.get_image() {
            let stamp = chrono::Utc::now().format("%Y%m%d-%H%M%SZ"); // UTC, as every time in disp's memory
            return self.load_rgba(img.width as u32, img.height as u32, img.bytes.into_owned(),
                &format!("clip-{}.png", stamp), "clipboard", None);
        }
        if let Ok(t) = cb.get_text() {
            let t = t.trim().trim_matches('"').to_string();
            let t = t.strip_prefix("file://").unwrap_or(&t).to_string();
            if !t.is_empty() && t.len() < 4096 && expand(&t).is_file() {
                return self.load_path(&t, "clipboard");
            }
        }
        Err("no image on the clipboard".into())
    }

    pub fn set_focus(&mut self, i: usize) {
        if self.focus != Some(i) {
            self.focus = Some(i);
            self.sel = None;
            self.fit = true;
            self.picks.clear();
        }
        if !self.shown.contains(&i) {
            self.shown.push(i); // focused = loaded in this session: on the strip
        }
        self.check(i);
    }

    /// A fresh session: the strip empties, nothing in focus (the catalog, marks and numbers stay).
    pub fn clear_session(&mut self) -> usize {
        let n = self.shown.len();
        self.shown.clear();
        self.focus = None;
        self.sel = None;
        self.picks.clear();
        self.banner = None;
        self.pixels.clear();
        self.layers.clear();
        self.recalled.clear();
        self.strip_to = None;
        n
    }

    /// Images a recall brought up: onto the strip (the focus stays where it is), tagged with their R numbers (the
    /// previous recall's tags go). Returns (keys shown, what could not be found).
    pub fn recall_onto_strip(&mut self, items: &[(Value, Vec<u32>)]) -> (Vec<String>, Vec<String>) {
        self.recalled.clear();
        let (mut shown, mut missing, mut first) = (vec![], vec![], None);
        for (who, ranks) in items {
            match self.resolve(Some(who)) {
                Ok(i) => {
                    if !self.shown.contains(&i) {
                        self.shown.push(i);
                    }
                    first.get_or_insert(i);
                    self.recalled.insert(i, ranks.clone());
                    shown.push(self.imgs[i].key());
                }
                Err(_) => missing.push(who.as_str().map(|s| s[..s.len().min(8)].to_string()).unwrap_or_else(|| who.to_string())),
            }
        }
        self.strip_to = first.and_then(|i| self.shown.iter().position(|&j| j == i));
        (shown, missing)
    }

    pub fn add_ann(&mut self, i: usize, mut a: Ann) -> u32 {
        let img = &mut self.imgs[i];
        if img.next_ann == 0 {
            img.next_ann = img.anns.iter().map(|a| a.id).max().unwrap_or(0) + 1;
        }
        a.id = img.next_ann;
        img.next_ann += 1;
        img.anns.push(a);
        let id = img.next_ann - 1;
        self.save_marks(i);
        id
    }

    /// Remove marks: the given ids, else all of them. Returns how many went.
    pub fn clear_anns(&mut self, i: usize, ids: Option<Vec<u32>>) -> usize {
        let img = &mut self.imgs[i];
        let before = img.anns.len();
        match ids {
            Some(ids) => img.anns.retain(|a| !ids.contains(&a.id)),
            None => img.anns.clear(),
        }
        let gone = before - img.anns.len();
        if gone > 0 {
            self.save_marks(i);
        }
        gone
    }

    pub fn crop(&mut self, i: usize, r: [f32; 4]) -> Result<(usize, bool), String> {
        let px = self.pixels(i)?;
        let [x, y, w, h] = clamp_rect(r, px.width(), px.height());
        let sub = image::imageops::crop_imm(px.as_ref(), x, y, w, h).to_image();
        let parent = &self.imgs[i];
        let stem = parent.name.rsplit_once('.').map(|(s, _)| s.to_string()).unwrap_or(parent.name.clone());
        let name = format!("{}-crop-{}_{}_{}x{}.png", stem, x, y, w, h);
        let psha = Some(parent.sha.clone());
        self.load_rgba(w, h, sub.into_raw(), &name, "crop", psha)
    }

    /// The view handed to the vision model: region (default whole image) scaled to fit `max`, marks and an
    /// optional grid burnt in. Coordinates in the result map back with x = x0 + ex / scale.
    /// With `out` the PNG is written there (save it); without, it comes back inline as base64 ("png_b64") and nothing
    /// is written: it can always be made again from the image, so it is not kept.
    pub fn export(&mut self, i: usize, region: Option<[f32; 4]>, max: u32, marks: bool, grid: f32, out: Option<PathBuf>)
        -> Result<Value, String> {
        let px = self.pixels(i)?;
        let (w, h) = (px.width(), px.height());
        let [x0, y0, rw, rh] = region.map(|r| clamp_rect(r, w, h)).unwrap_or([0, 0, w, h]);
        let sub = image::imageops::crop_imm(px.as_ref(), x0, y0, rw, rh).to_image();
        let max = max.clamp(64, 4096);
        let scale = (max as f32 / rw.max(rh) as f32).min(if region.is_some() { 4.0 } else { 1.0 });
        let (ew, eh) = (((rw as f32 * scale).round() as u32).max(1), ((rh as f32 * scale).round() as u32).max(1));
        let mut ex = if (ew, eh) == (rw, rh) { sub } else { image::imageops::resize(&sub, ew, eh, FilterType::Triangle) };
        let (fx, fy) = (x0 as f32, y0 as f32);
        if grid > 0.0 {
            raster::draw_grid(&mut ex, grid, fx, fy, scale);
        }
        let mut drawn = 0;
        if marks {
            let map = |p: [f32; 2]| [(p[0] - fx) * scale, (p[1] - fy) * scale];
            for a in self.imgs[i].anns.clone() {
                raster::draw_ann(&mut ex, &a, &map, scale);
                drawn += 1;
            }
        }
        let mut res = json!({"w": ew, "h": eh, "scale": scale, "x0": x0, "y0": y0, "region": [x0, y0, rw, rh],
                             "marks": drawn, "n": self.imgs[i].n});
        match out {
            Some(path) => {
                if let Some(d) = path.parent() {
                    let _ = fs::create_dir_all(d);
                }
                ex.save(&path).map_err(|e| format!("cannot write {}: {}", path.display(), e))?;
                res["path"] = json!(path.to_string_lossy());
            }
            None => {
                let mut png = std::io::Cursor::new(Vec::new());
                DynamicImage::ImageRgba8(ex).write_to(&mut png, image::ImageFormat::Png).map_err(|e| e.to_string())?;
                res["png_b64"] = json!(b64(png.get_ref()));
            }
        }
        Ok(res)
    }

    pub fn pixel(&mut self, i: usize, x: f32, y: f32) -> Result<[u8; 4], String> {
        let px = self.pixels(i)?;
        if x < 0.0 || y < 0.0 || x >= px.width() as f32 || y >= px.height() as f32 {
            return Err(format!("({}, {}) is outside {}x{}", x, y, px.width(), px.height()));
        }
        Ok(raster::px(&px, x as u32, y as u32))
    }

    /// Colour statistics of a region: mean / min / max per channel and the dominant colours.
    pub fn stats(&mut self, i: usize, region: Option<[f32; 4]>) -> Result<Value, String> {
        let px = self.pixels(i)?;
        let [x0, y0, w, h] = region.map(|r| clamp_rect(r, px.width(), px.height())).unwrap_or([0, 0, px.width(), px.height()]);
        let step = (((w as u64 * h as u64) as f64 / 250_000.0).sqrt().ceil() as u32).max(1);
        let (mut sum, mut mn, mut mx, mut n) = ([0u64; 4], [255u8; 4], [0u8; 4], 0u64);
        let mut hist: HashMap<u16, u64> = HashMap::new();
        let mut y = y0;
        while y < y0 + h {
            let mut x = x0;
            while x < x0 + w {
                let p = raster::px(&px, x, y);
                for c in 0..4 {
                    sum[c] += p[c] as u64;
                    mn[c] = mn[c].min(p[c]);
                    mx[c] = mx[c].max(p[c]);
                }
                let key = ((p[0] as u16 >> 4) << 8) | ((p[1] as u16 >> 4) << 4) | (p[2] as u16 >> 4);
                *hist.entry(key).or_default() += 1;
                n += 1;
                x += step;
            }
            y += step;
        }
        let n = n.max(1);
        let mean: Vec<u8> = sum.iter().map(|s| (s / n) as u8).collect();
        let mut top: Vec<(u16, u64)> = hist.into_iter().collect();
        top.sort_by(|a, b| b.1.cmp(&a.1));
        let dominant: Vec<Value> = top
            .iter()
            .take(6)
            .map(|(k, c)| {
                let (r, g, b) = (((k >> 8) & 15) * 17, ((k >> 4) & 15) * 17, (k & 15) * 17);
                json!({"hex": format!("#{:02x}{:02x}{:02x}", r, g, b), "share": (*c as f64 / n as f64 * 1000.0).round() / 10.0})
            })
            .collect();
        Ok(json!({"region": [x0, y0, w, h], "mean": mean, "min": mn, "max": mx,
                  "mean_hex": format!("#{:02x}{:02x}{:02x}", mean[0], mean[1], mean[2]), "dominant": dominant,
                  "sampled_every": step}))
    }

    /// Snap a box seen by the eye (often off by tens of pixels) to the pixels: the background is the commonest colour
    /// on the border of a search window around the seed, the object the commonest colour in the seed that is not the
    /// background; the answer is the bounding box of the connected blob of that colour overlapping the seed (glyphs
    /// joined by a dilation of `gap` pixels for text). `edge`: the blob touches the window (look again around it).
    pub fn snap(&mut self, i: usize, seed: [f32; 4], tol: f32, gap: u32, text: bool) -> Result<Value, String> {
        let px = self.pixels(i)?;
        let (iw, ih) = (px.width(), px.height());
        let [sx, sy, sw, sh] = clamp_rect(seed, iw, ih);
        let ex = (sw as f32 * 0.6).max(24.0) as u32;
        let ey = if text { (sh as f32 * 2.0).max(24.0) as u32 } else { (sh as f32 * 0.6).max(24.0) as u32 };
        let (wx, wy) = (sx.saturating_sub(ex), sy.saturating_sub(ey));
        let (ww, wh) = ((sx + sw + ex).min(iw) - wx, (sy + sh + ey).min(ih) - wy);
        let quant = |p: [u8; 4]| ((p[0] as u16 >> 4) << 8) | ((p[1] as u16 >> 4) << 4) | (p[2] as u16 >> 4);
        let dist = |p: [u8; 4], c: [f32; 3]| {
            ((p[0] as f32 - c[0]).powi(2) + (p[1] as f32 - c[1]).powi(2) + (p[2] as f32 - c[2]).powi(2)).sqrt()
        };
        let top = |h: &HashMap<u16, (u64, [u64; 3])>| -> Option<[f32; 3]> {
            h.values().max_by_key(|v| v.0).map(|(n, s)| [s[0] as f32 / *n as f32, s[1] as f32 / *n as f32, s[2] as f32 / *n as f32])
        };
        let add = |h: &mut HashMap<u16, (u64, [u64; 3])>, p: [u8; 4]| {
            let e = h.entry(quant(p)).or_insert((0, [0; 3]));
            e.0 += 1;
            for c in 0..3 {
                e.1[c] += p[c] as u64;
            }
        };
        let mut ring = HashMap::new();
        for x in wx..wx + ww {
            add(&mut ring, raster::px(&px, x, wy));
            add(&mut ring, raster::px(&px, x, wy + wh - 1));
        }
        for y in wy..wy + wh {
            add(&mut ring, raster::px(&px, wx, y));
            add(&mut ring, raster::px(&px, wx + ww - 1, y));
        }
        let bg = top(&ring).ok_or("empty window")?;
        let mut inside = HashMap::new();
        for y in sy..sy + sh {
            for x in sx..sx + sw {
                let p = raster::px(&px, x, y);
                if dist(p, bg) > tol {
                    add(&mut inside, p);
                }
            }
        }
        let obj = top(&inside).ok_or("nothing in the box differs from the background")?;
        let (w, h) = (ww as usize, wh as usize);
        let mut mask = vec![false; w * h];
        for y in 0..h {
            for x in 0..w {
                mask[y * w + x] = dist(raster::px(&px, wx + x as u32, wy + y as u32), obj) <= tol;
            }
        }
        // dilate (separable box) so the glyphs of a word / the words of a line join
        let mut grown = mask.clone();
        let g = gap as usize;
        if g > 0 {
            let mut tmp = vec![false; w * h];
            for y in 0..h {
                let mut last: isize = -100000;
                for x in 0..w {
                    if mask[y * w + x] {
                        last = x as isize;
                    }
                    if x as isize - last <= g as isize {
                        tmp[y * w + x] = true;
                    }
                }
                let mut next: isize = 100000;
                for x in (0..w).rev() {
                    if mask[y * w + x] {
                        next = x as isize;
                    }
                    if next - x as isize <= g as isize {
                        tmp[y * w + x] = true;
                    }
                }
            }
            for x in 0..w {
                let mut last: isize = -100000;
                for y in 0..h {
                    if tmp[y * w + x] {
                        last = y as isize;
                    }
                    grown[y * w + x] = y as isize - last <= g as isize;
                }
                let mut next: isize = 100000;
                for y in (0..h).rev() {
                    if tmp[y * w + x] {
                        next = y as isize;
                    }
                    if next - y as isize <= g as isize {
                        grown[y * w + x] = true;
                    }
                }
            }
        }
        // connected components of the grown mask; the one with most object pixels inside the seed wins
        let mut label = vec![0u32; w * h];
        let (mut best, mut best_score, mut next_label) = (0u32, 0u64, 1u32);
        let (s0x, s0y, s1x, s1y) = ((sx - wx) as usize, (sy - wy) as usize, (sx - wx + sw) as usize, (sy - wy + sh) as usize);
        let mut stack = Vec::new();
        let mut comps: Vec<(u64, [usize; 4], bool)> = vec![(0, [0; 4], false)];
        for start in 0..w * h {
            if !grown[start] || label[start] != 0 {
                continue;
            }
            let id = next_label;
            next_label += 1;
            let (mut score, mut count, mut bb, mut edge) = (0u64, 0u64, [usize::MAX, usize::MAX, 0, 0], false);
            label[start] = id;
            stack.push(start);
            while let Some(k) = stack.pop() {
                let (x, y) = (k % w, k / w);
                if mask[k] {
                    count += 1;
                    if x >= s0x && x < s1x && y >= s0y && y < s1y {
                        score += 1;
                    }
                    bb = [bb[0].min(x), bb[1].min(y), bb[2].max(x), bb[3].max(y)];
                    if x == 0 || y == 0 || x == w - 1 || y == h - 1 {
                        edge = true;
                    }
                }
                let mut push = |n: usize| {
                    if grown[n] && label[n] == 0 {
                        label[n] = id;
                        stack.push(n);
                    }
                };
                if x > 0 { push(k - 1); }
                if x + 1 < w { push(k + 1); }
                if y > 0 { push(k - w); }
                if y + 1 < h { push(k + w); }
            }
            comps.push((count, bb, edge));
            if score > best_score {
                best_score = score;
                best = id;
            }
        }
        if best == 0 {
            return Err("no blob of the object's colour in the box".into());
        }
        let (count, bb, edge) = comps[best as usize];
        let (bx, by, bw, bh) = (wx + bb[0] as u32, wy + bb[1] as u32, (bb[2] - bb[0] + 1) as u32, (bb[3] - bb[1] + 1) as u32);
        let hex = |c: [f32; 3]| format!("#{:02x}{:02x}{:02x}", c[0] as u8, c[1] as u8, c[2] as u8);
        Ok(json!({"box": [bx, by, bw, bh], "pixels": count, "fill": count as f64 / (bw as f64 * bh as f64),
                  "color": hex(obj), "bg": hex(bg), "edge": edge, "window": [wx, wy, ww, wh]}))
    }

    /// The part of the focused image on screen, in image pixels.
    pub fn visible(&self) -> Option<[f32; 4]> {
        let i = self.focus?;
        let img = &self.imgs[i];
        let z = self.zoom.max(1e-6);
        let (hw, hh) = (self.view_size[0] / 2.0 / z, self.view_size[1] / 2.0 / z);
        let x0 = (self.center[0] - hw).max(0.0);
        let y0 = (self.center[1] - hh).max(0.0);
        let x1 = (self.center[0] + hw).min(img.w as f32);
        let y1 = (self.center[1] + hh).min(img.h as f32);
        Some([x0.round(), y0.round(), (x1 - x0).max(0.0).round(), (y1 - y0).max(0.0).round()])
    }

    /// Zoom and centre that show the whole focused image.
    pub fn fit_view(&mut self) {
        if let Some(i) = self.focus {
            let (w, h) = (self.imgs[i].w as f32, self.imgs[i].h as f32);
            self.zoom = (self.view_size[0] / w).min(self.view_size[1] / h).max(1e-4);
            self.center = [w / 2.0, h / 2.0];
        }
    }

    /// Show a region of the focused image as large as the view allows.
    pub fn zoom_to(&mut self, r: [f32; 4]) {
        let (w, h) = (r[2].abs().max(1.0), r[3].abs().max(1.0));
        self.fit = false;
        self.zoom = (self.view_size[0] / w).min(self.view_size[1] / h).clamp(0.01, 64.0);
        self.center = [r[0] + w / 2.0, r[1] + h / 2.0];
    }

    pub fn state(&self) -> Value {
        let focus = self.focus.map(|i| self.info(i));
        let marks: Vec<Value> = self
            .focus
            .map(|i| self.imgs[i].anns.iter().map(|a| json!(a)).collect())
            .unwrap_or_default();
        json!({"focus": focus, "count": self.shown.len(), "known": self.imgs.len(), "zoom": self.zoom, "fit": self.fit,
               "visible": self.visible(), "selection": self.sel, "tool": self.tool.name(), "marks": marks,
               "port": self.port, "home": self.home.to_string_lossy(), "clients": self.clients,
               "grid": self.grid, "picks": self.picks,
               "layer": self.focus.and_then(|i| self.layers.get(&self.imgs[i].sha)).map(|l| l.nodes.len()),
               "window": self.window, "passes": [self.passes.0, self.passes.1]})
    }
}

/// Standard base64 (RFC 4648, padded): PNGs travel inline on the socket.
pub fn b64(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut s = String::with_capacity((data.len() + 2) / 3 * 4);
    for c in data.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        s.push(T[(n >> 18) as usize & 63] as char);
        s.push(T[(n >> 12) as usize & 63] as char);
        s.push(if c.len() > 1 { T[(n >> 6) as usize & 63] as char } else { '=' });
        s.push(if c.len() > 2 { T[n as usize & 63] as char } else { '=' });
    }
    s
}

pub fn expand(p: &str) -> PathBuf {
    let p = p.trim();
    if let Some(rest) = p.strip_prefix("~/") {
        if let Some(h) = std::env::var_os("HOME") {
            return Path::new(&h).join(rest);
        }
    }
    PathBuf::from(p)
}
