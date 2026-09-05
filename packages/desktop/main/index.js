const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');

let mainWindow = null;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    webPreferences: {
      preload: path.join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    // Windows 使用原生标题栏（frame: true）；macOS 保留 hidden 风格
    titleBarStyle: process.platform === 'darwin' ? 'hidden' : 'default',
    title: '投标智航 / TenderPilot',
    frame: true,
  });

  if (process.env.NODE_ENV === 'development') {
    // Vite dev server 可能尚未就绪，失败后重试，避免白屏
    let retries = 0;
    const loadDevUrl = () => {
      mainWindow.loadURL('http://localhost:5173').catch(() => {});
    };
    mainWindow.webContents.on('did-fail-load', (_e, code, desc, url, isMainFrame) => {
      if (!isMainFrame) return;
      if (retries < 10) {
        retries += 1;
        setTimeout(loadDevUrl, 2000);
      }
    });
    loadDevUrl();
    if (process.env.BMP_ELECTRON_DEVTOOLS === '1') {
      mainWindow.webContents.openDevTools();
    }
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'));
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.whenReady().then(() => {
  app.setName('投标智航 / TenderPilot');
  createWindow();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  if (mainWindow === null) {
    createWindow();
  }
});
