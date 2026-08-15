// ─── 状态 ───
let config = null;
let proxyRunning = false;

// ─── 渲染（纯函数，不依赖 DOM 引用） ───

function updateStatus($status, $toggleBtn, port) {
  if (proxyRunning) {
    $status.textContent = `● 运行中  :${port}`;
    $status.className = "running";
    $toggleBtn.textContent = "停止代理";
  } else {
    $status.textContent = "● 已停止";
    $status.className = "stopped";
    $toggleBtn.textContent = "启动代理";
  }
}

function renderTable($tbody) {
  $tbody.innerHTML = "";
  config.endpoints.forEach((ep, i) => {
    const tr = document.createElement("tr");

    // 名称
    const tdName = document.createElement("td");
    tdName.className = "col-name";
    const inpName = document.createElement("input");
    inpName.type = "text";
    inpName.value = ep.name;
    inpName.addEventListener("input", () => {
      const oldDefault = config.endpoints[i].name === config.default_name;
      config.endpoints[i].name = inpName.value.trim();
      if (oldDefault) config.default_name = config.endpoints[i].name;
    });
    tdName.appendChild(inpName);
    tr.appendChild(tdName);

    // BaseURL
    const tdUrl = document.createElement("td");
    tdUrl.className = "col-url";
    const inpUrl = document.createElement("input");
    inpUrl.type = "url";
    inpUrl.value = ep.base_url;
    inpUrl.addEventListener("input", () => {
      config.endpoints[i].base_url = inpUrl.value.trim().replace(/\/$/, "");
    });
    tdUrl.appendChild(inpUrl);
    tr.appendChild(tdUrl);

    // 默认
    const tdDef = document.createElement("td");
    tdDef.className = "col-default";
    tdDef.style.textAlign = "center";
    const cbDef = document.createElement("input");
    cbDef.type = "checkbox";
    cbDef.checked = ep.name === config.default_name;
    cbDef.addEventListener("change", () => {
      if (cbDef.checked) {
        config.default_name = config.endpoints[i].name;
        renderTable($tbody);
      } else {
        cbDef.checked = true;
      }
    });
    tdDef.appendChild(cbDef);
    tr.appendChild(tdDef);

    // 启用
    const tdEn = document.createElement("td");
    tdEn.className = "col-enabled";
    tdEn.style.textAlign = "center";
    const cbEn = document.createElement("input");
    cbEn.type = "checkbox";
    cbEn.checked = ep.enabled;
    cbEn.addEventListener("change", () => {
      config.endpoints[i].enabled = cbEn.checked;
    });
    tdEn.appendChild(cbEn);
    tr.appendChild(tdEn);

    // 复制
    const tdCopy = document.createElement("td");
    tdCopy.className = "col-copy";
    tdCopy.style.textAlign = "center";
    const btnCopy = document.createElement("button");
    btnCopy.className = "copy-btn";
    btnCopy.textContent = "⎘";
    btnCopy.title = "复制代理地址";
    btnCopy.addEventListener("click", async () => {
      const addr = `http://127.0.0.1:${config.port}/${ep.name}/v1`;
      try {
        await navigator.clipboard.writeText(addr);
        btnCopy.textContent = "✓";
        btnCopy.classList.add("copied");
        setTimeout(() => {
          btnCopy.textContent = "⎘";
          btnCopy.classList.remove("copied");
        }, 1500);
      } catch {
        alert("复制失败，请手动复制");
      }
    });
    tdCopy.appendChild(btnCopy);
    tr.appendChild(tdCopy);

    // 删除
    const tdDel = document.createElement("td");
    tdDel.className = "col-delete";
    tdDel.style.textAlign = "center";
    const btnDel = document.createElement("button");
    btnDel.className = "del-btn";
    btnDel.textContent = "−";
    btnDel.addEventListener("click", () => {
      if (config.endpoints.length <= 1) {
        alert("至少保留一个 baseURL");
        return;
      }
      const removed = config.endpoints.splice(i, 1)[0];
      if (config.default_name === removed.name && config.endpoints.length > 0) {
        config.default_name = config.endpoints[0].name;
      }
      renderTable($tbody);
    });
    tdDel.appendChild(btnDel);
    tr.appendChild(tdDel);

    $tbody.appendChild(tr);
  });
}

function renderAll($port, $status, $toggleBtn, $tbody) {
  $port.value = config.port;
  updateStatus($status, $toggleBtn, config.port);
  renderTable($tbody);
}

// ─── 启动（DOM 就绪后执行） ───

document.addEventListener("DOMContentLoaded", async () => {
  // 调试：打印 Tauri API 结构
  console.log("[MiMo] window.__TAURI__ type:", typeof window.__TAURI__);
  if (window.__TAURI__) {
    console.log("[MiMo] window.__TAURI__ keys:", Object.keys(window.__TAURI__));
    for (const k of Object.keys(window.__TAURI__)) {
      console.log(`[MiMo]   ${k} keys:`, typeof window.__TAURI__[k] === "object" ? Object.keys(window.__TAURI__[k]) : typeof window.__TAURI__[k]);
    }
  }

  if (!window.__TAURI__ || !window.__TAURI__.core) {
    document.body.innerHTML = `<div style="padding:20px;color:red;font-family:monospace;">
      <h2>Tauri API 不可用</h2>
      <p>window.__TAURI__: ${typeof window.__TAURI__}</p>
      <p>请确认 tauri.conf.json 中 withGlobalTauri: true</p>
    </div>`;
    return;
  }

  const { invoke } = window.__TAURI__.core;
  const { listen } = window.__TAURI__.event;

  // DOM 元素
  const $port = document.getElementById("port");
  const $status = document.getElementById("status");
  const $toggleBtn = document.getElementById("toggle-btn");
  const $tbody = document.getElementById("endpoint-tbody");
  const $addBtn = document.getElementById("add-btn");
  const $reloadBtn = document.getElementById("reload-btn");
  const $saveBtn = document.getElementById("save-btn");

  // ─── 初始化 ───
  // 先注册监听器，防止 auto-start 的 proxy-status 事件丢失
  listen("proxy-status", (event) => {
    proxyRunning = event.payload;
    updateStatus($status, $toggleBtn, $port.value);
  });

  config = await invoke("get_config");
  renderAll($port, $status, $toggleBtn, $tbody);

  // 主动查询当前代理状态（auto-start 可能已经启动）
  proxyRunning = await invoke("get_proxy_status");
  updateStatus($status, $toggleBtn, config.port);

  // ─── 按钮事件 ───

  // 操作成功后在按钮上短暂显示结果，避免"点了没反应"的观感
  function flashBtn(btn, text, ms = 1600) {
    const orig = btn.textContent;
    btn.textContent = text;
    btn.disabled = true;
    setTimeout(() => {
      btn.textContent = orig;
      btn.disabled = false;
    }, ms);
  }

  $toggleBtn.addEventListener("click", async () => {
    $toggleBtn.disabled = true;
    try {
      if (proxyRunning) {
        await invoke("stop_proxy");
      } else {
        await invoke("start_proxy");
      }
    } catch (e) {
      console.error(e);
    } finally {
      $toggleBtn.disabled = false;
    }
  });

  $addBtn.addEventListener("click", () => {
    const existing = new Set(config.endpoints.map((e) => e.name));
    let base = "mimo";
    let name = base;
    let i = 1;
    while (existing.has(name)) {
      name = `${base}${i}`;
      i++;
    }
    config.endpoints.push({
      name: name,
      base_url: "https://api.xiaomimimo.com/v1",
      enabled: true,
    });
    renderTable($tbody);
    $tbody.parentElement.scrollTop = $tbody.parentElement.scrollHeight;
  });

  $reloadBtn.addEventListener("click", async () => {
    try {
      config = await invoke("get_config");
      renderAll($port, $status, $toggleBtn, $tbody);
      flashBtn($reloadBtn, "✓ 已重载");
    } catch (e) {
      console.error(e);
      alert(`重载失败: ${e}`);
    }
  });

  $saveBtn.addEventListener("click", async () => {
    const port = parseInt($port.value, 10);
    if (isNaN(port) || port < 1 || port > 65535) {
      alert("端口必须是 1-65535 的整数");
      return;
    }

    const nameRe = /^[a-zA-Z0-9_-]+$/;
    const reserved = new Set(["v1", "models", "chat", "health", ""]);
    const seen = new Set();
    for (const ep of config.endpoints) {
      const n = ep.name.trim();
      if (!n) { alert("名称不能为空"); return; }
      if (!nameRe.test(n)) { alert(`名称 "${n}" 只能包含字母、数字、下划线和短横线`); return; }
      if (reserved.has(n)) { alert(`名称 "${n}" 是保留字`); return; }
      if (seen.has(n)) { alert(`名称 "${n}" 重复`); return; }
      seen.add(n);
      if (!ep.base_url) { alert(`"${n}" 的 BaseURL 不能为空`); return; }
      if (!ep.base_url.startsWith("http://") && !ep.base_url.startsWith("https://")) {
        alert(`"${n}" 的 BaseURL 必须以 http:// 或 https:// 开头`);
        return;
      }
    }

    config.port = port;
    if (!config.endpoints.some((e) => e.name === config.default_name)) {
      config.default_name = config.endpoints[0].name;
    }

    try {
      const wasRunning = proxyRunning;
      $saveBtn.disabled = true;
      config = await invoke("save_config_cmd", { cfg: config });
      if (wasRunning) {
        await invoke("restart_proxy");
      }
      renderAll($port, $status, $toggleBtn, $tbody);
      flashBtn($saveBtn, wasRunning ? "✓ 已保存并应用" : "✓ 已保存（启动后生效）");
    } catch (e) {
      console.error(e);
      alert(`保存失败: ${e}`);
      $saveBtn.disabled = false;
    }
  });

  $port.addEventListener("change", () => {
    config.port = parseInt($port.value, 10) || config.port;
  });
});