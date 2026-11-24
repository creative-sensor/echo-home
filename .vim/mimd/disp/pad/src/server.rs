//! The control socket: newline-delimited JSON on 127.0.0.1:<port>. Any number of clients (the disp agent, a
//! shell with `nc`); each request gets its answer, every client gets the events.
//!
//! client -> pad   {"id":N,"op":..., ...}                        pad -> client   {"id":N,"ok":true,...} | {"id":N,"ok":false,"err":..}
//!   hello | list | state | raise | quit | screenshot {path?}   (the window as the user sees it; inline without path)
//!   open {path}            paste                    focus {image}
//!   view {image?, fit?|zoom?|zoom_by?|center?:[x,y]|region?:[x,y,w,h]}
//!   select {region}|{clear:true}                    tool {name}           prompt {text, secs?}
//!   crop {image?, region?}            -> a new image (the selection if no region)
//!   mark {image?, shape, points:[[x,y]..], text?, color?:[r,g,b,a], width?, by?}  -> {id}
//!   unmark {image?, ids?}             (no ids: all marks)
//!   export {image?, region?, max?, marks?, grid?, path?}   -> {w, h, scale, x0, y0, region} + path (written there)
//!                                     or png_b64 (no path: the PNG inline, nothing written)
//!   pixel {image?, x, y}              stats {image?, region?}
//!   layer {image?, nodes:[{id,type,label,x,y,box,exact}], links:[{a,b,rel,kind}]} | {image?, clear:true}
//!                                     disp's map as a grid layer (memory only)      grid {on?} (toggle)   picks
//!   snap {image?, seed:[x,y,w,h], text?, tol?, gap?}  -> {box, pixels, fill, color, bg, edge}  (a rough box to the pixels)
//! pad -> clients (events, no id)
//!   {"ev":"loaded","image":{..},"new":b,"by":"pad"|"client"}   {"ev":"focus","image":{..}}
//!   {"ev":"click","n":..,"x":..,"y":..,"rgba":[..],"button":..}  {"ev":"select","n":..,"region":[..]}
//!   {"ev":"mark","n":..,"mark":{..}}  {"ev":"unmark","n":..}  {"ev":"ask","text":..,"n":..,"region":..,"refs":[..]}
//!   {"ev":"pick","n":..,"id":..,"on":b,"added":[..],"ids":[..]}  {"ev":"grid","on":b}  {"ev":"view",..}  {"ev":"bye"}

use crate::model::{Ann, Layer, Link, Model, Node, Tool, AGENT_COLOR};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

type Shared = Arc<Mutex<Model>>;

pub struct Hub {
    clients: Mutex<Vec<(u64, Arc<Mutex<TcpStream>>)>>,
    next: AtomicU64,
}

impl Hub {
    pub fn new() -> Hub {
        Hub { clients: Mutex::new(vec![]), next: AtomicU64::new(1) }
    }

    pub fn broadcast(&self, v: Value) {
        let line = v.to_string() + "\n";
        let mut cl = self.clients.lock().unwrap();
        cl.retain(|(_, s)| s.lock().map(|mut s| s.write_all(line.as_bytes()).is_ok()).unwrap_or(false));
    }

    fn count(&self) -> usize {
        self.clients.lock().map(|c| c.len()).unwrap_or(0)
    }
}

fn region(v: &Value) -> Option<[f32; 4]> {
    let a = v.as_array()?;
    if a.len() != 4 {
        return None;
    }
    let f: Vec<f32> = a.iter().filter_map(|x| x.as_f64().map(|f| f as f32)).collect();
    (f.len() == 4).then(|| [f[0], f[1], f[2], f[3]])
}

fn point(v: &Value) -> Option<[f32; 2]> {
    let a = v.as_array()?;
    let f: Vec<f32> = a.iter().filter_map(|x| x.as_f64().map(|f| f as f32)).collect();
    (f.len() >= 2).then(|| [f[0], f[1]])
}

fn color(v: &Value) -> Option<[u8; 4]> {
    if let Some(s) = v.as_str() {
        let s = s.trim_start_matches('#');
        let named = match s.to_ascii_lowercase().as_str() {
            "red" => Some([255, 64, 64, 255]),
            "green" => Some([64, 220, 96, 255]),
            "blue" => Some([64, 140, 255, 255]),
            "yellow" | "amber" => Some([255, 196, 0, 255]),
            "white" => Some([255, 255, 255, 255]),
            "black" => Some([0, 0, 0, 255]),
            "cyan" => Some([0, 220, 230, 255]),
            "magenta" | "pink" => Some([230, 80, 200, 255]),
            "orange" => Some([255, 140, 0, 255]),
            _ => None,
        };
        if named.is_some() {
            return named;
        }
        if s.len() == 6 || s.len() == 8 {
            let b = |i: usize| u8::from_str_radix(&s[i..i + 2], 16).ok();
            return Some([b(0)?, b(2)?, b(4)?, if s.len() == 8 { b(6)? } else { 255 }]);
        }
        return None;
    }
    let a = v.as_array()?;
    let f: Vec<u8> = a.iter().filter_map(|x| x.as_u64().map(|u| u.min(255) as u8)).collect();
    match f.len() {
        3 => Some([f[0], f[1], f[2], 255]),
        4 => Some([f[0], f[1], f[2], f[3]]),
        _ => None,
    }
}

/// Run one request against the model. Events it causes are returned too, to be broadcast after the lock is gone.
fn handle(m: &mut Model, req: &Value, events: &mut Vec<Value>) -> Result<Value, String> {
    let op = req.get("op").and_then(|v| v.as_str()).unwrap_or("");
    let image = req.get("image").or_else(|| req.get("n"));
    match op {
        "hello" => Ok(json!({"pad": env!("CARGO_PKG_VERSION"), "home": m.home.to_string_lossy(), "port": m.port,
                             "count": m.imgs.len(), "focus": m.focus.map(|i| m.info(i))})),
        "list" => {
            let last = req.get("last").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
            let from = if last > 0 { m.imgs.len().saturating_sub(last) } else { 0 };
            let list: Vec<Value> = (from..m.imgs.len()).map(|i| m.info(i)).collect();
            Ok(json!({"images": list, "count": m.imgs.len(), "focus": m.focus.map(|i| m.imgs[i].n)}))
        }
        "state" => Ok(m.state()),
        "raise" => {
            m.raise = true;
            Ok(json!({}))
        }
        "quit" => {
            m.quit = true;
            Ok(json!({}))
        }
        "open" => {
            let path = req.get("path").and_then(|v| v.as_str()).ok_or("open needs \"path\"")?;
            let (i, new) = m.load_path(path, req.get("source").and_then(|v| v.as_str()).unwrap_or("file"))?;
            let info = m.info(i);
            events.push(json!({"ev": "loaded", "image": info, "new": new, "by": "client"}));
            Ok(json!({"image": info, "new": new}))
        }
        "paste" => {
            let (i, new) = m.paste()?;
            let info = m.info(i);
            events.push(json!({"ev": "loaded", "image": info, "new": new, "by": "client"}));
            Ok(json!({"image": info, "new": new}))
        }
        "focus" => {
            let i = m.resolve(image)?;
            m.set_focus(i);
            let info = m.info(i);
            events.push(json!({"ev": "focus", "image": info}));
            Ok(json!({"image": info}))
        }
        "view" => {
            if image.is_some() {
                let i = m.resolve(image)?;
                if m.focus != Some(i) {
                    m.set_focus(i);
                    events.push(json!({"ev": "focus", "image": m.info(i)}));
                }
            }
            m.focus.ok_or("no image in focus")?;
            if req.get("fit").and_then(|v| v.as_bool()).unwrap_or(false) {
                m.fit = true;
                m.fit_view();
            } else if let Some(r) = req.get("region").and_then(region) {
                m.zoom_to(r);
            } else {
                if let Some(z) = req.get("zoom").and_then(|v| v.as_f64()) {
                    m.fit = false;
                    m.zoom = (z as f32).clamp(0.01, 64.0);
                }
                if let Some(z) = req.get("zoom_by").and_then(|v| v.as_f64()) {
                    m.fit = false;
                    m.zoom = (m.zoom * z as f32).clamp(0.01, 64.0);
                }
                if let Some(c) = req.get("center").and_then(point) {
                    m.fit = false;
                    m.center = c;
                }
            }
            Ok(json!({"zoom": m.zoom, "center": m.center, "visible": m.visible()}))
        }
        "select" => {
            let i = m.resolve(image)?;
            if req.get("clear").and_then(|v| v.as_bool()).unwrap_or(false) {
                m.sel = None;
                return Ok(json!({"selection": null}));
            }
            let r = req.get("region").and_then(region).ok_or("select needs \"region\": [x,y,w,h]")?;
            let [x, y, w, h] = crate::model::clamp_rect(r, m.imgs[i].w, m.imgs[i].h);
            m.set_focus(i);
            m.sel = Some([x as f32, y as f32, w as f32, h as f32]);
            Ok(json!({"selection": [x, y, w, h]}))
        }
        "tool" => {
            let name = req.get("name").and_then(|v| v.as_str()).unwrap_or("");
            m.tool = Tool::from_name(name).ok_or_else(|| format!("no tool '{}'", name))?;
            Ok(json!({"tool": m.tool.name()}))
        }
        "prompt" => {
            let text = req.get("text").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let secs = req.get("secs").and_then(|v| v.as_f64()).unwrap_or(30.0);
            m.banner = if text.is_empty() { None } else { Some((text, crate::model::now() + secs)) };
            Ok(json!({}))
        }
        "crop" => {
            let i = m.resolve(image)?;
            let r = req.get("region").and_then(region).or(if m.focus == Some(i) { m.sel } else { None })
                .ok_or("crop needs \"region\" (or a selection on the pad)")?;
            let (j, new) = m.crop(i, r)?;
            let info = m.info(j);
            events.push(json!({"ev": "loaded", "image": info, "new": new, "by": "client"}));
            Ok(json!({"image": info, "new": new}))
        }
        "mark" => {
            let i = m.resolve(image)?;
            let shape = req.get("shape").and_then(|v| v.as_str()).unwrap_or("point").to_ascii_lowercase();
            let shape = match shape.as_str() {
                "box" | "rectangle" | "square" => "rect".to_string(),
                "circle" | "oval" => "ellipse".to_string(),
                "label" | "note" => "text".to_string(),
                "dot" | "marker" | "pin" => "point".to_string(),
                "path" | "stroke" | "freehand" => "pen".to_string(),
                s => s.to_string(),
            };
            let mut pts: Vec<[f32; 2]> = req.get("points").and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(point).collect()).unwrap_or_default();
            if let Some(r) = req.get("region").and_then(region) {
                pts = vec![[r[0], r[1]], [r[0] + r[2], r[1] + r[3]]];
            }
            if pts.is_empty() {
                return Err("mark needs \"points\": [[x,y], ...] (or \"region\")".into());
            }
            let need = if matches!(shape.as_str(), "rect" | "ellipse" | "arrow" | "line") { 2 } else { 1 };
            if pts.len() < need {
                return Err(format!("a {} mark needs {} points", shape, need));
            }
            let by = req.get("by").and_then(|v| v.as_str()).unwrap_or("agent").to_string();
            let ann = Ann {
                id: 0,
                shape,
                pts,
                color: req.get("color").and_then(color).unwrap_or(AGENT_COLOR),
                width: req.get("width").and_then(|v| v.as_f64()).map(|f| f as f32).unwrap_or(3.0),
                text: req.get("text").and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(|s| s.to_string()),
                by,
            };
            let id = m.add_ann(i, ann);
            let mark = m.imgs[i].anns.last().map(|a| json!(a)).unwrap_or(Value::Null);
            events.push(json!({"ev": "mark", "n": m.imgs[i].n, "mark": mark}));
            Ok(json!({"id": id, "n": m.imgs[i].n}))
        }
        "unmark" => {
            let i = m.resolve(image)?;
            let ids = req.get("ids").and_then(|v| v.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_u64().map(|u| u as u32)).collect::<Vec<u32>>());
            let gone = m.clear_anns(i, ids);
            events.push(json!({"ev": "unmark", "n": m.imgs[i].n}));
            Ok(json!({"removed": gone}))
        }
        "export" => {
            let i = m.resolve(image)?;
            let r = req.get("region").and_then(region);
            let max = req.get("max").and_then(|v| v.as_u64()).unwrap_or(896) as u32;
            let marks = req.get("marks").and_then(|v| v.as_bool()).unwrap_or(true);
            let grid = req.get("grid").and_then(|v| v.as_f64()).unwrap_or(0.0) as f32;
            let path = req.get("path").and_then(|v| v.as_str()).map(crate::model::expand);
            m.export(i, r, max, marks, grid, path)
        }
        "pixel" => {
            let i = m.resolve(image)?;
            let x = req.get("x").and_then(|v| v.as_f64()).ok_or("pixel needs x")? as f32;
            let y = req.get("y").and_then(|v| v.as_f64()).ok_or("pixel needs y")? as f32;
            let p = m.pixel(i, x, y)?;
            Ok(json!({"n": m.imgs[i].n, "x": x as u32, "y": y as u32, "rgba": p,
                      "hex": format!("#{:02x}{:02x}{:02x}", p[0], p[1], p[2])}))
        }
        "layer" => {
            // disp's map of an image as dots and links (kept in memory, by sha)
            let i = m.resolve(image)?;
            let sha = m.imgs[i].sha.clone();
            if req.get("clear").and_then(|v| v.as_bool()).unwrap_or(false) {
                m.layers.remove(&sha);
                return Ok(json!({"nodes": 0, "links": 0}));
            }
            let nodes: Vec<Node> = serde_json::from_value(req.get("nodes").cloned().unwrap_or(json!([])))
                .map_err(|e| format!("bad nodes: {}", e))?;
            let links: Vec<Link> = serde_json::from_value(req.get("links").cloned().unwrap_or(json!([])))
                .map_err(|e| format!("bad links: {}", e))?;
            let (nn, nl) = (nodes.len(), links.len());
            if m.focus == Some(i) {
                m.picks.retain(|p| nodes.iter().any(|n| &n.id == p));
            }
            m.layers.insert(sha, Layer { nodes, links });
            Ok(json!({"nodes": nn, "links": nl}))
        }
        "grid" => {
            m.grid = req.get("on").and_then(|v| v.as_bool()).unwrap_or(!m.grid);
            events.push(json!({"ev": "grid", "on": m.grid, "n": m.focus.map(|i| m.imgs[i].n)}));
            Ok(json!({"on": m.grid}))
        }
        "picks" => Ok(json!({"picks": m.picks})),
        "snap" => {
            let i = m.resolve(image)?;
            let seed = req.get("seed").and_then(region).ok_or("snap needs \"seed\": [x,y,w,h]")?;
            let text = req.get("text").and_then(|v| v.as_bool()).unwrap_or(false);
            let tol = req.get("tol").and_then(|v| v.as_f64()).unwrap_or(48.0) as f32;
            let gap = req.get("gap").and_then(|v| v.as_u64())
                .unwrap_or(if text { ((seed[3] * 0.5) as u64).max(2) } else { 1 }) as u32;
            m.snap(i, seed, tol, gap, text)
        }
        "stats" => {
            let i = m.resolve(image)?;
            let r = req.get("region").and_then(region);
            m.stats(i, r)
        }
        "" => Err("missing \"op\"".into()),
        other => Err(format!("unknown op '{}'", other)),
    }
}

fn client_loop(stream: TcpStream, id: u64, model: Shared, hub: Arc<Hub>, ctx: eframe::egui::Context) {
    let Ok(read) = stream.try_clone() else { return };
    let writer = Arc::new(Mutex::new(stream));
    hub.clients.lock().unwrap().push((id, writer.clone()));
    if let Ok(mut m) = model.lock() {
        m.clients = hub.count();
    }
    ctx.request_repaint();
    let reader = BufReader::new(read);
    for line in reader.lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        let (resp, events) = match serde_json::from_str::<Value>(&line) {
            Ok(req) if req.get("op").and_then(|v| v.as_str()) == Some("screenshot") => {
                let mut out = screenshot(&req, &model, &ctx);
                out["id"] = req.get("id").cloned().unwrap_or(Value::Null);
                (out, vec![])
            }
            Ok(req) => {
                let mut events = vec![];
                let res = match model.lock() {
                    Ok(mut m) => handle(&mut m, &req, &mut events),
                    Err(_) => Err("pad state poisoned".into()),
                };
                let mut out = match res {
                    Ok(Value::Object(mut o)) => {
                        o.insert("ok".into(), json!(true));
                        Value::Object(o)
                    }
                    Ok(v) => json!({"ok": true, "value": v}),
                    Err(e) => json!({"ok": false, "err": e}),
                };
                out["id"] = req.get("id").cloned().unwrap_or(Value::Null);
                (out, events)
            }
            Err(e) => (json!({"ok": false, "err": format!("bad JSON: {}", e)}), vec![]),
        };
        let ok = writer.lock().map(|mut w| w.write_all((resp.to_string() + "\n").as_bytes()).is_ok()).unwrap_or(false);
        for ev in events {
            hub.broadcast(ev);
        }
        ctx.request_repaint();
        if !ok {
            break;
        }
    }
    hub.clients.lock().unwrap().retain(|(cid, _)| *cid != id);
    if let Ok(mut m) = model.lock() {
        m.clients = hub.count();
    }
    ctx.request_repaint();
}

/// The pad window as the user sees it, saved as PNG (physical pixels). Needs the window to draw a frame.
fn screenshot(req: &Value, model: &Shared, ctx: &eframe::egui::Context) -> Value {
    let (tx, rx) = std::sync::mpsc::channel();
    match model.lock() {
        Ok(mut m) => m.shot = Some(tx),
        Err(_) => return json!({"ok": false, "err": "pad state poisoned"}),
    }
    let path = req.get("path").and_then(|v| v.as_str()).map(crate::model::expand);
    // the pixels are read on the frame after the one that sends the command: keep frames coming until they arrive
    let end = std::time::Instant::now() + std::time::Duration::from_secs(5);
    let got = loop {
        ctx.request_repaint();
        match rx.recv_timeout(std::time::Duration::from_millis(100)) {
            Ok(img) => break Ok(img),
            Err(_) if std::time::Instant::now() < end => continue,
            Err(e) => break Err(e),
        }
    };
    match got {
        Ok(img) => {
            let [w, h] = img.size;
            let raw: Vec<u8> = img.pixels.iter().flat_map(|c| c.to_array()).collect();
            let Some(img) = image::RgbaImage::from_raw(w as u32, h as u32, raw) else {
                return json!({"ok": false, "err": "bad screenshot buffer"});
            };
            match path {
                Some(path) => match img.save(&path) {
                    Ok(()) => json!({"ok": true, "path": path.to_string_lossy(), "w": w, "h": h}),
                    Err(e) => json!({"ok": false, "err": format!("cannot write {}: {}", path.display(), e)}),
                },
                None => {
                    let mut png = std::io::Cursor::new(Vec::new());
                    match image::DynamicImage::ImageRgba8(img).write_to(&mut png, image::ImageFormat::Png) {
                        Ok(()) => json!({"ok": true, "png_b64": crate::model::b64(png.get_ref()), "w": w, "h": h}),
                        Err(e) => json!({"ok": false, "err": e.to_string()}),
                    }
                }
            }
        }
        Err(_) => json!({"ok": false, "err": "the pad window drew no frame (minimized or hidden?)"}),
    }
}

pub fn serve(listener: TcpListener, model: Shared, hub: Arc<Hub>, ctx: eframe::egui::Context) {
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            let _ = stream.set_nodelay(true);
            let id = hub.next.fetch_add(1, Ordering::Relaxed);
            let (model, hub, ctx) = (model.clone(), hub.clone(), ctx.clone());
            std::thread::spawn(move || client_loop(stream, id, model, hub, ctx));
        }
    });
}
