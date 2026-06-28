'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('miot', {
  // Resolves to e.g. "http://127.0.0.1:54321" once the Python sidecar is up.
  getBackendUrl: () => ipcRenderer.invoke('get-backend-url'),
  // "main" or "quick"
  getWindowRole: () => ipcRenderer.invoke('get-window-role'),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  getAutoLaunch: () => ipcRenderer.invoke('get-auto-launch'),
  setAutoLaunch: (enabled) => ipcRenderer.invoke('set-auto-launch', enabled),
  showMain: () => ipcRenderer.invoke('show-main'),
  hideWindow: () => ipcRenderer.invoke('hide-window'),
  broadcast: (channel) => ipcRenderer.invoke('broadcast', channel),
  oauthLogin: (authUrl) => ipcRenderer.invoke('oauth-login', authUrl),
  on: (channel, cb) => ipcRenderer.on(channel, () => cb()),
});
