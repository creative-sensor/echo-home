const { contextBridge, ipcRenderer } = require('electron')

window.addEventListener("DOMContentLoaded", () => {
    console.log("DOM loaded");
});



contextBridge.exposeInMainWorld('ipc', {
  node:  process.versions.node,
  chrome: () => process.versions.chrome,
  electron: () => process.versions.electron,
  ETRON_APP: process.env.ETRON_APP  ,
  shell_exec: (cmd) => {  return ipcRenderer.invoke('shell_exec',cmd)  }
});


