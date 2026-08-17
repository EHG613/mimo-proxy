use serde::{Deserialize, Serialize};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::{
    menu::{MenuBuilder, MenuItem, MenuItemBuilder},
    tray::{MouseButton, TrayIcon, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager,
};

// ─── 配置结构（与 Python client/config.py 兼容） ───

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Endpoint {
    pub name: String,
    pub base_url: String,
    pub enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProxyConfig {
    pub host: String,
    pub port: u16,
    pub auto_start: bool,
    #[serde(rename = "cache_max_size")]
    pub cache_max_size: usize,
    #[serde(rename = "cache_ttl")]
    pub cache_ttl: u64,
    #[serde(rename = "default_name")]
    pub default_name: String,
    pub endpoints: Vec<Endpoint>,
}

impl Default for ProxyConfig {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".into(),
            port: 8899,
            auto_start: true,
            cache_max_size: 2000,
            cache_ttl: 7200,
            default_name: "default".into(),
            endpoints: vec![Endpoint {
                name: "default".into(),
                base_url: "https://one-api-test.liangyihui.net:8080/v1".into(),
                enabled: true,
            }],
        }
    }
}

// ─── 代理进程 + 托盘状态 ───

pub struct AppState {
    pub child: Option<Child>,
    pub tray: Option<TrayIcon>,
    // 托盘菜单项句柄：状态文本和启动/停止按钮需要跟随代理状态刷新
    pub status_item: Option<MenuItem<tauri::Wry>>,
    pub toggle_item: Option<MenuItem<tauri::Wry>>,
}

// ─── 配置读写 ───

fn config_path() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    let dir = std::path::PathBuf::from(&home).join(".mimo-proxy");
    std::fs::create_dir_all(&dir).ok();
    let cfg = dir.join("config.json");
    // 一次性迁移：旧版目录为 ~/Library/Application Support/MiMoProxy（保留不删，作为回滚备份）
    if !cfg.exists() {
        let legacy = std::path::PathBuf::from(&home)
            .join("Library/Application Support/MiMoProxy/config.json");
        if legacy.exists() {
            let _ = std::fs::copy(&legacy, &cfg);
        }
    }
    cfg
}

fn load_config() -> ProxyConfig {
    let path = config_path();
    match std::fs::read_to_string(&path) {
        Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
        Err(_) => {
            let cfg = ProxyConfig::default();
            save_config(&cfg);
            cfg
        }
    }
}

fn save_config(cfg: &ProxyConfig) {
    let path = config_path();
    if let Ok(json) = serde_json::to_string_pretty(cfg) {
        std::fs::write(&path, json).ok();
    }
}

// ─── 托盘菜单更新 ───

fn update_tray(app: &AppHandle) {
    let cfg = load_config();
    let state = app.state::<Mutex<AppState>>();
    if let Ok(st) = state.lock() {
        let running = st.child.is_some();
        // 刷新菜单项文本：状态行 + 启动/停止按钮（只改 tooltip 会导致菜单永远停在初始文案）
        if let Some(ref item) = st.status_item {
            let _ = item.set_text(if running {
                format!("状态: 运行中 :{}", cfg.port)
            } else {
                "状态: 已停止".to_string()
            });
        }
        if let Some(ref item) = st.toggle_item {
            let _ = item.set_text(if running { "停止代理" } else { "启动代理" });
        }
        if let Some(ref tray) = st.tray {
            tray.set_icon_as_template(true).ok();
            tray.set_tooltip(Some(if running {
                format!("MiMo Proxy - 运行中 :{}", cfg.port)
            } else {
                "MiMo Proxy - 已停止".into()
            }))
            .ok();
        }
    };
}

// macOS：打开配置窗口时切回常规模式（Dock 图标出现）；窗口关闭时切到 Accessory 隐藏 Dock
fn show_config_window(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
    if let Some(window) = app.get_webview_window("config") {
        let _ = window.unminimize();
        window.show().ok();
        window.set_focus().ok();
    }
}

// ─── IPC 命令 ───

#[tauri::command]
fn get_config() -> ProxyConfig {
    load_config()
}

#[tauri::command]
fn save_config_cmd(cfg: ProxyConfig) -> Result<ProxyConfig, String> {
    save_config(&cfg);
    Ok(cfg)
}

#[tauri::command]
fn start_proxy(app: AppHandle) -> Result<(), String> {
    let state = app.state::<Mutex<AppState>>();
    let mut st = state.lock().map_err(|e| e.to_string())?;
    if st.child.is_some() {
        return Err("代理已在运行".into());
    }

    let config_dir = config_path().parent().unwrap().to_path_buf();

    let repo_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf();

    // sidecar 解析：开发用仓库 .venv 直跑 client 源码（改 Python 代码无需重新打包）；
    // 生产包用 PyInstaller 独立可执行文件（Resources/sidecar/mimo-proxy-sidecar/）。
    // 注意顺序不能反：tauri dev 的 resource_dir 也能命中 src-tauri/resources，
    // 若 sidecar 优先会导致 dev 永远跑旧二进制。
    let mut cmd = {
        let venv_python = repo_dir.join(".venv/bin/python");
        if venv_python.exists() {
            let mut c = Command::new(&venv_python);
            c.args(["-m", "client", "--cli"])
                .current_dir(&repo_dir)
                .env("PYTHONPATH", &repo_dir);
            c
        } else {
            let sidecar_bin = app
                .path()
                .resource_dir()
                .ok()
                .map(|dir| dir.join("sidecar/mimo-proxy-sidecar/mimo-proxy-sidecar"))
                .filter(|p| p.is_file());
            if let Some(bin) = sidecar_bin {
                let mut c = Command::new(&bin);
                c.args(["--cli"]).current_dir(bin.parent().unwrap_or(std::path::Path::new("/")));
                c
            } else {
                return Err(
                    "未找到代理 sidecar：开发环境缺少 .venv/bin/python，生产包缺少 resources/sidecar（运行 npm run build:sidecar 重新打包）"
                        .into(),
                );
            }
        }
    };

    let child = cmd
        // stdin 接管道但从不写入：本应用终止（含被强杀）时内核关闭管道写端，
        // sidecar 读到 EOF 立即退出，零延迟清理；sidecar 另有 ppid watchdog 轮询兜底。
        // 注意：写端 fd 由 Child 句柄持有，句柄存活期间管道保持打开。
        .stdin(Stdio::piped())
        .env("MIMO_PROXY_CONFIG_DIR", config_dir.to_string_lossy().to_string())
        .spawn()
        .map_err(|e| format!("启动代理失败: {}", e))?;

    st.child = Some(child);
    drop(st);

    // 后台线程监控进程退出（轮询方式，避免持有锁阻塞）
    // 注意：判断与清空必须在同一次锁内完成，否则可能与 start_proxy 竞态，
    // 把新启动的子进程句柄误清空（进程泄漏、UI 显示已停止但端口仍被占用）。
    let monitor_app = app.clone();
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(std::time::Duration::from_millis(500));
            let exited = {
                let state = monitor_app.state::<Mutex<AppState>>();
                let Ok(mut st) = state.lock() else { break };
                match st.child.as_mut() {
                    Some(child) => {
                        if child.try_wait().ok().flatten().is_some() {
                            // 同一把锁内确认退出并清空，避免 TOCTOU
                            st.child = None;
                            true
                        } else {
                            false
                        }
                    }
                    // 已被 stop_proxy 接管（它自己会发事件），安静退出，
                    // 避免基于过期观察补发 proxy-status:false 覆盖新状态
                    None => break,
                }
            };
            if exited {
                let _ = monitor_app.emit("proxy-status", false);
                update_tray(&monitor_app);
                break;
            }
        }
    });

    let _ = app.emit("proxy-status", true);
    update_tray(&app);
    Ok(())
}

#[tauri::command]
fn stop_proxy(app: AppHandle) -> Result<(), String> {
    let child = {
        let state = app.state::<Mutex<AppState>>();
        let mut st = state.lock().map_err(|e| e.to_string())?;
        st.child.take()
    };
    if let Some(mut child) = child {
        // kill/wait 放在锁外执行，避免持锁阻塞 UI；
        // 即使 kill 失败（如进程已退出）也继续 wait 回收，不向调用方抛错导致状态不同步
        if let Err(e) = child.kill() {
            eprintln!("停止代理 kill 失败: {}", e);
        }
        let _ = child.wait();
    }
    let _ = app.emit("proxy-status", false);
    update_tray(&app);
    Ok(())
}

#[tauri::command]
fn restart_proxy(app: AppHandle) -> Result<(), String> {
    let _ = stop_proxy(app.clone());
    std::thread::sleep(std::time::Duration::from_millis(500));
    start_proxy(app)
}

#[tauri::command]
fn get_proxy_status(app: AppHandle) -> bool {
    let state = app.state::<Mutex<AppState>>();
    state.lock().map(|s| s.child.is_some()).unwrap_or(false)
}

// ─── 应用入口 ───

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(Mutex::new(AppState {
            child: None,
            tray: None,
            status_item: None,
            toggle_item: None,
        }))
        .invoke_handler(tauri::generate_handler![
            get_config,
            save_config_cmd,
            start_proxy,
            stop_proxy,
            restart_proxy,
            get_proxy_status,
        ])
        .setup(|app| {
            let handle = app.handle().clone();

            // 构建托盘菜单
            let status_item = MenuItemBuilder::with_id("status", "状态: 已停止")
                .enabled(false)
                .build(app)?;
            let toggle_item = MenuItemBuilder::with_id("toggle", "启动代理").build(app)?;
            let config_item = MenuItemBuilder::with_id("config", "打开配置窗口…").build(app)?;
            let sep = tauri::menu::PredefinedMenuItem::separator(app)?;
            let quit_item = MenuItemBuilder::with_id("quit", "退出").build(app)?;

            let menu = MenuBuilder::new(app)
                .item(&status_item)
                .item(&toggle_item)
                .item(&config_item)
                .item(&sep)
                .item(&quit_item)
                .build()?;

            let tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("MiMo Proxy")
                .menu(&menu)
                .on_menu_event({
                    move |app, event| {
                        let id = event.id().as_ref();
                        match id {
                            "toggle" => {
                                let app_handle = app.clone();
                                std::thread::spawn(move || {
                                    let state = app_handle.state::<Mutex<AppState>>();
                                    let running = state.lock().map(|s| s.child.is_some()).unwrap_or(false);
                                    if running {
                                        let _ = stop_proxy(app_handle.clone());
                                    } else {
                                        let _ = start_proxy(app_handle.clone());
                                    }
                                });
                            }
                            "config" => {
                                show_config_window(app);
                            }
                            "quit" => {
                                let app_handle = app.clone();
                                std::thread::spawn(move || {
                                    let _ = stop_proxy(app_handle.clone());
                                    app_handle.exit(0);
                                });
                            }
                            _ => {}
                        }
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        ..
                    } = event
                    {
                        show_config_window(tray.app_handle());
                    }
                })
                .build(app)?;

            // 存储 tray 与菜单项句柄
            {
                let state = app.state::<Mutex<AppState>>();
                if let Ok(mut st) = state.lock() {
                    st.tray = Some(tray);
                    st.status_item = Some(status_item);
                    st.toggle_item = Some(toggle_item);
                };
            }

            update_tray(&handle);

            // 自动启动代理
            let cfg = load_config();
            if cfg.auto_start {
                let h = handle.clone();
                std::thread::spawn(move || {
                    let _ = start_proxy(h);
                });
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "config" {
                    window.hide().ok();
                    // macOS：窗口关闭后切到 Accessory 模式，Dock 图标隐藏，仅保留菜单栏托盘
                    #[cfg(target_os = "macos")]
                    let _ = window
                        .app_handle()
                        .set_activation_policy(tauri::ActivationPolicy::Accessory);
                    api.prevent_close();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            match event {
                // macOS：点击程序坞图标（或 Finder 重新打开）时，显示并聚焦配置窗口。
                // 窗口平时是 hide() 隐藏的，不处理 Reopen 就会表现为"点了没反应"
                tauri::RunEvent::Reopen { .. } => {
                    show_config_window(app);
                }
                tauri::RunEvent::Exit => {
                    // 退出时清理 Python sidecar（锁外 kill，避免持锁阻塞）
                    let child = app
                        .state::<Mutex<AppState>>()
                        .lock()
                        .ok()
                        .and_then(|mut st| st.child.take());
                    if let Some(mut child) = child {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
                _ => {}
            }
        });
}
