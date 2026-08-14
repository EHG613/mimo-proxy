use serde::{Deserialize, Serialize};
use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::{
    menu::{MenuBuilder, MenuItemBuilder},
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
}

// ─── 配置读写 ───

fn config_path() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    let path = std::path::PathBuf::from(home)
        .join("Library/Application Support/MiMoProxy");
    std::fs::create_dir_all(&path).ok();
    path.join("config.json")
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
    let state = app.state::<Mutex<AppState>>();
    let running = state.lock().map(|s| s.child.is_some()).unwrap_or(false);
    let cfg = load_config();

    if let Ok(st) = state.lock() {
        if let Some(ref tray) = st.tray {
            tray.set_icon_as_template(true).ok();
            tray.set_tooltip(Some(
                if running {
                    format!("MiMo Proxy - 运行中 :{}", cfg.port)
                } else {
                    "MiMo Proxy - 已停止".into()
                }
            )).ok();
        }
    };
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
    // 生产包将 client 作为资源放入 resource_dir/client；开发时使用仓库目录。
    let client_dir = app
        .path()
        .resource_dir()
        .ok()
        .map(|dir| dir.join("client"))
        .filter(|dir| dir.join("__main__.py").exists())
        .unwrap_or_else(|| repo_dir.join("client"));
    let work_dir = client_dir
        .parent()
        .ok_or_else(|| "代理资源目录无效".to_string())?
        .to_path_buf();

    // 使用项目目录下的 .venv/bin/python，回退到系统 python3。
    let venv_python = repo_dir.join(".venv/bin/python");
    let python = if venv_python.exists() {
        venv_python
    } else {
        std::path::PathBuf::from("python3")
    };

    let child = Command::new(&python)
        .args(["-m", "client", "--cli"])
        .current_dir(&work_dir)
        .env("PYTHONPATH", &work_dir)
        .env("MIMO_PROXY_CONFIG_DIR", config_dir.to_string_lossy().to_string())
        .spawn()
        .map_err(|e| format!("启动代理失败 ({}): {}", python.display(), e))?;

    st.child = Some(child);
    drop(st);

    // 后台线程监控进程退出（轮询方式，避免持有锁阻塞）
    let monitor_app = app.clone();
    std::thread::spawn(move || {
        loop {
            let state = monitor_app.state::<Mutex<AppState>>();
            let exited = if let Ok(mut st) = state.lock() {
                if let Some(ref mut child) = st.child {
                    child.try_wait().ok().flatten().is_some()
                } else {
                    true
                }
            } else {
                true
            };
            if exited {
                if let Ok(mut st) = monitor_app.state::<Mutex<AppState>>().lock() {
                    st.child = None;
                }
                let _ = monitor_app.emit("proxy-status", false);
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(500));
        }
    });

    let _ = app.emit("proxy-status", true);
    update_tray(&app);
    Ok(())
}

#[tauri::command]
fn stop_proxy(app: AppHandle) -> Result<(), String> {
    let state = app.state::<Mutex<AppState>>();
    let mut st = state.lock().map_err(|e| e.to_string())?;
    if let Some(mut child) = st.child.take() {
        child.kill().map_err(|e| format!("停止代理失败: {}", e))?;
    }
    drop(st); // 必须在调用 update_tray 前释放锁，否则 update_tray 内 lock() 会死锁
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
                                if let Some(window) = app.get_webview_window("config") {
                                    window.show().ok();
                                    window.set_focus().ok();
                                }
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
                        if let Some(window) = tray.app_handle().get_webview_window("config") {
                            window.show().ok();
                            window.set_focus().ok();
                        }
                    }
                })
                .build(app)?;

            // 存储 tray 句柄
            {
                let state = app.state::<Mutex<AppState>>();
                if let Ok(mut st) = state.lock() {
                    st.tray = Some(tray);
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
                    api.prevent_close();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                // 退出时清理 Python sidecar
                let state = app.state::<Mutex<AppState>>();
                if let Ok(mut st) = state.lock() {
                    if let Some(mut child) = st.child.take() {
                        let _ = child.kill();
                    }
                }; // 分号确保 MutexGuard 在 state 之前释放
            }
        });
}
