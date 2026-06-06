'use strict'

const { contextBridge, ipcRenderer } = require('electron')

// Expose only what the renderer needs — keeps contextIsolation intact
contextBridge.exposeInMainWorld('APP', {
  port:    parseInt(process.env.GLIDER_PORT     || '5757', 10),
  baseDir: process.env.GLIDER_BASE_DIR          || '',
  shellOpen: (filePath) => ipcRenderer.invoke('shell-open', filePath),
})
