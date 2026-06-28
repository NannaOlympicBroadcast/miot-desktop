'use strict';

const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

// ---------------------------------------------------------------------------
// Configuration (overridable via environment variables)
// ---------------------------------------------------------------------------
const PYTHON =
  process.env.MIOT_PYTHON || 'C:/ProgramData/miniconda3/python.exe';
const BACKEND_SCRIPT = path.join(__dirname, 'backend', 'server.py');
const TRAY_ICON = path.join(__dirname, 'assets', 'tray.png');

// In a packaged build the backend is a PyInstaller onedir bundle shipped under
// resources/backend/. In dev we run the script with the local Python.
function backendCommand() {
  if (app.isPackaged) {
    const exe = path.join(process.resourcesPath, 'backend', 'miot-backend.exe');
    return { cmd: exe, args: [] };
  }
  return { cmd: PYTHON, args: [BACKEND_SCRIPT] };
}

let mainWindow = null;
let quickWindow = null;
let tray = null;
let backendProc = null;
let backendPort = null;
let backendReady = null; // Promise resolving to the port

// ---------------------------------------------------------------------------
// Spawn the Python sidecar and wait for it to announce its port
// ---------------------------------------------------------------------------
function startBackend() {
  backendReady = new Promise((resolve, reject) => {
    const env = Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' });
    const { cmd, args } = backendCommand();
    if (app.isPackaged && !fs.existsSync(cmd)) {
      reject(new Error(`未找到后端可执行文件: ${cmd}`));
      return;
    }
    backendProc = spawn(cmd, args, { env });

    let settled = false;
    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error('后端启动超时 (30s)。请确认 Python 与 miot_kit 路径正确。'));
      }
    }, 30000);

    backendProc.stdout.on('data', (data) => {
      const text = data.toString();
      const m = text.match(/MIOT_BACKEND_PORT=(\d+)/);
      if (m && !settled) {
        settled = true;
        clearTimeout(timeout);
        backendPort = parseInt(m[1], 10);
        resolve(backendPort);
      }
      process.stdout.write(`[backend] ${text}`);
    });
    backendProc.stderr.on('data', (data) => process.stderr.write(`[backend] ${data.toString()}`));
    backendProc.on('error', (err) => {
      if (!settled) { settled = true; clearTimeout(timeout); reject(err); }
    });
    backendProc.on('exit', (code) => {
      if (!settled) { settled = true; clearTimeout(timeout); reject(new Error(`后端进程退出，code=${code}`)); }
      backendProc = null;
    });
  });
  return backendReady;
}

function stopBackend() {
  if (backendProc) {
    try { backendProc.kill(); } catch (e) { /* ignore */ }
    backendProc = null;
  }
}

// ---------------------------------------------------------------------------
// IPC
// ---------------------------------------------------------------------------
ipcMain.handle('get-backend-url', async () => {
  const port = await backendReady;
  return `http://127.0.0.1:${port}`;
});
ipcMain.handle('get-window-role', (e) => {
  const win = BrowserWindow.fromWebContents(e.sender);
  return win === quickWindow ? 'quick' : 'main';
});
ipcMain.handle('open-external', (e, url) => shell.openExternal(url));
ipcMain.handle('get-auto-launch', () => {
  try { return app.getLoginItemSettings().openAtLogin; } catch (e) { return false; }
});
ipcMain.handle('set-auto-launch', (e, enabled) => {
  try {
    app.setLoginItemSettings({
      openAtLogin: !!enabled,
      // Start hidden to the tray on login.
      args: ['--hidden'],
    });
    return app.getLoginItemSettings().openAtLogin;
  } catch (err) {
    return false;
  }
});
ipcMain.handle('show-main', () => showMain());
ipcMain.handle('hide-window', (e) => {
  const win = BrowserWindow.fromWebContents(e.sender);
  if (win) win.hide();
});
// Renderer notifies that login succeeded -> other windows refresh.
ipcMain.handle('broadcast', (e, channel) => {
  BrowserWindow.getAllWindows().forEach((w) => {
    if (w.webContents !== e.sender) w.webContents.send(channel);
  });
});

// ---------------------------------------------------------------------------
// In-app OAuth login: open the Xiaomi authorize page in a child window and
// capture the http://127.0.0.1 redirect URL ourselves, so the user never has
// to copy/paste it from an external browser.
// ---------------------------------------------------------------------------
ipcMain.handle('oauth-login', (e, authUrl) => {
  return new Promise((resolve) => {
    const parent = BrowserWindow.fromWebContents(e.sender);
    const loginWindow = new BrowserWindow({
      width: 480,
      height: 680,
      parent: parent || undefined,
      modal: !!parent,
      title: '登录小米账号',
      autoHideMenuBar: true,
      webPreferences: { contextIsolation: true, nodeIntegration: false },
    });
    loginWindow.removeMenu();

    let settled = false;
    const finish = (redirectUrl) => {
      if (settled) return;
      settled = true;
      resolve(redirectUrl);
      if (!loginWindow.isDestroyed()) loginWindow.close();
    };

    const checkRedirect = (url) => {
      if (/^https?:\/\/127\.0\.0\.1(\/|:|$|\?)/.test(url)) finish(url);
    };

    loginWindow.webContents.on('will-redirect', (ev, url) => checkRedirect(url));
    loginWindow.webContents.on('will-navigate', (ev, url) => checkRedirect(url));
    loginWindow.webContents.on('did-fail-load', (ev, code, desc, url) => checkRedirect(url));
    loginWindow.on('closed', () => finish(null));

    loginWindow.loadURL(authUrl);
  });
});

// ---------------------------------------------------------------------------
// Windows
// ---------------------------------------------------------------------------
function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1180,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#f4f5f7',
    title: '米家桌面端',
    show: !process.argv.includes('--hidden'),
    icon: fs.existsSync(TRAY_ICON) ? TRAY_ICON : undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.removeMenu();
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  // Close button -> hide to tray instead of quitting.
  mainWindow.on('close', (e) => {
    if (!app.isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
}

function showMain() {
  if (!mainWindow) createMainWindow();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function toggleQuickWindow() {
  if (quickWindow && !quickWindow.isDestroyed()) {
    if (quickWindow.isVisible()) { quickWindow.hide(); return; }
    quickWindow.show();
    quickWindow.focus();
    return;
  }
  quickWindow = new BrowserWindow({
    width: 340,
    height: 460,
    resizable: false,
    frame: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    backgroundColor: '#ffffff',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  quickWindow.loadFile(path.join(__dirname, 'renderer', 'quick.html'));
  // Hide on blur so it behaves like a popup panel.
  quickWindow.on('blur', () => { if (quickWindow && !quickWindow.webContents.isDevToolsOpened()) quickWindow.hide(); });
  // Position near tray (bottom-right).
  const { screen } = require('electron');
  const area = screen.getPrimaryDisplay().workAreaSize;
  quickWindow.setPosition(area.width - 360, area.height - 480);
}

function createTray() {
  let img = nativeImage.createFromPath(TRAY_ICON);
  if (img.isEmpty()) {
    // fallback 1x1 so Tray still constructs
    img = nativeImage.createEmpty();
  }
  tray = new Tray(img);
  tray.setToolTip('米家桌面端');
  const menu = Menu.buildFromTemplate([
    { label: '显示主界面', click: showMain },
    { label: '快捷控制面板', click: toggleQuickWindow },
    { type: 'separator' },
    { label: '退出', click: () => { app.isQuitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
  // Double-click -> main window; single left-click -> quick panel.
  tray.on('double-click', showMain);
  tray.on('click', toggleQuickWindow);
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
app.whenReady().then(async () => {
  // Validate the right backend target for the current mode (exe vs script).
  const target = app.isPackaged ? backendCommand().cmd : BACKEND_SCRIPT;
  if (!fs.existsSync(target)) {
    dialog.showErrorBox(
      '启动失败',
      app.isPackaged
        ? `未找到后端可执行文件: ${target}`
        : `未找到后端脚本: ${target}`);
    app.quit();
    return;
  }
  startBackend().catch((err) => {
    dialog.showErrorBox('后端启动失败', String(err && err.message ? err.message : err));
  });
  createMainWindow();
  createTray();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
    else showMain();
  });
});

// Don't quit when all windows are hidden/closed — we live in the tray.
app.on('window-all-closed', (e) => {
  // Intentionally do nothing on Windows/Linux: stay in tray.
  if (process.platform === 'darwin') return;
});

app.on('before-quit', () => { app.isQuitting = true; });
app.on('quit', stopBackend);
