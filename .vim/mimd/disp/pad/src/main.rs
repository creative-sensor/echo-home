//! disppad: the image pad of the disp agent (~/echo-home/.vim/mimd/vimpage_disp).
//!
//!   disppad [--port 7791] [--home ~/echo-home/memory/disp] [image ...]
//!
//! One pad per port: started again while one runs, it hands its images to the running pad, raises it and exits.
//! The disp agent (disp.py) drives it through the control socket (server.rs); the user through the window (ui.rs).

mod model;
mod raster;
mod server;
mod ui;

use eframe::egui;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};

const USAGE: &str = "usage: disppad [--port N] [--home DIR] [image ...]
  --port N    control socket on 127.0.0.1:N (default $DISP_PAD_PORT or 7791)
  --home DIR  image memory (default $DISP_HOME or ~/echo-home/memory/disp)
A pad already running on the port gets the images and comes to the front.";

fn main() {
    let mut port: u16 = std::env::var("DISP_PAD_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(7791);
    let mut home = std::env::var("DISP_HOME").unwrap_or_else(|_| "~/echo-home/memory/disp".into());
    let mut files: Vec<String> = vec![];
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--port" => port = args.next().and_then(|p| p.parse().ok()).unwrap_or(port),
            "--home" => home = args.next().unwrap_or(home),
            "-h" | "--help" => {
                println!("{}", USAGE);
                return;
            }
            "--version" => {
                println!("disppad {}", env!("CARGO_PKG_VERSION"));
                return;
            }
            _ => files.push(a),
        }
    }
    let abs = |f: &str| std::fs::canonicalize(model::expand(f)).map(|p| p.to_string_lossy().to_string()).unwrap_or(f.to_string());

    let listener = match TcpListener::bind(("127.0.0.1", port)) {
        Ok(l) => l,
        Err(e) => {
            // a pad runs there already: hand it the images
            match TcpStream::connect(("127.0.0.1", port)) {
                Ok(s) => {
                    let mut w = s.try_clone().expect("socket");
                    let mut r = BufReader::new(s);
                    let mut reqs: Vec<Value> = files.iter().enumerate()
                        .map(|(i, f)| json!({"id": i + 1, "op": "open", "path": abs(f)})).collect();
                    reqs.push(json!({"id": 0, "op": "raise"}));
                    for q in &reqs {
                        let _ = w.write_all((q.to_string() + "\n").as_bytes());
                    }
                    let mut answered = 0;
                    let mut line = String::new();
                    while answered < reqs.len() {
                        line.clear();
                        if r.read_line(&mut line).unwrap_or(0) == 0 {
                            break;
                        }
                        if let Ok(v) = serde_json::from_str::<Value>(&line) {
                            if v.get("id").is_some() && v.get("ev").is_none() {
                                answered += 1;
                                if v["ok"] == json!(false) {
                                    eprintln!("disppad: {}", v["err"].as_str().unwrap_or("error"));
                                }
                            }
                        }
                    }
                    return;
                }
                Err(_) => {
                    eprintln!("disppad: cannot listen on 127.0.0.1:{}: {}", port, e);
                    std::process::exit(1);
                }
            }
        }
    };

    let model = Arc::new(Mutex::new(model::Model::new(model::expand(&home), port)));
    {
        let mut m = model.lock().unwrap();
        for f in &files {
            if let Err(e) = m.load_path(&abs(f), "file") {
                eprintln!("disppad: {}", e);
            }
        }
        if m.focus.is_none() && !m.imgs.is_empty() {
            let last = m.imgs.len() - 1;
            m.set_focus(last);
        }
    }
    let hub = Arc::new(server::Hub::new());
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("DISPAD")
            .with_app_id("disppad")
            .with_inner_size([1180.0, 860.0])
            .with_min_inner_size([520.0, 380.0])
            .with_drag_and_drop(true),
        ..Default::default()
    };
    let res = eframe::run_native(
        "DISPAD",
        options,
        Box::new(move |cc| {
            cc.egui_ctx.set_theme(egui::ThemePreference::Dark);
            server::serve(listener, model.clone(), hub.clone(), cc.egui_ctx.clone());
            Ok(Box::new(ui::PadApp::new(model, hub)))
        }),
    );
    if let Err(e) = res {
        eprintln!("disppad: {}", e);
        std::process::exit(1);
    }
}
