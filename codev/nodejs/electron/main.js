const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const util = require('node:util');
const exec = util.promisify(require('node:child_process').exec);

app.on("ready", () => {
    const mainWindow = new BrowserWindow({
        width : 1200,
        height: 800,
        webPreferences: {
            webviewTag: true,
            preload: path.join(__dirname, 'preload.js'),
            //nodeIntegration: true,
            //contextIsolation: true,
        }
    });

    mainWindow.loadFile(`${process.env.ETRON_APP}/index.html`);
});


app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
        loadMainWindow();
    }
});

ipcMain.handle("shell_exec", (event, cmd) => {
     return exec(cmd);
});
