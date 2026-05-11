# markdown : table format

--------------------------------

### 0 DESCRIPTION

### 1 SOLUTION

### 2 NOTES

- [Releases · denoland/deno · GitHub](https://github.com/denoland/deno/releases)

- [GitHub - toppair/peek.nvim: Markdown preview plugin for Neovim · GitHub](https://github.com/toppair/peek.nvim)

### 3 TEST/VERIFICATION

### 4 JOURNAL

--------------------------------

It looks like the build process didn't finish or failed silently. The `main.bundle.js` file is the compiled output of the plugin's frontend; if it's missing, `peek.nvim` can't launch the preview window.

This usually happens because **Deno** wasn't able to run the build task during the `PlugInstall` phase.

### How to Fix It

**1. Verify Deno is installed**
Open your terminal (PowerShell or Command Prompt) and run:

```bash
deno --version

```

If this returns an error, you need to install Deno from [deno.land](https://deno.land/) first.

**2. Manually trigger the build**
Sometimes the automated "do" hook in vim-plug fails due to permission issues or pathing on Windows. You can run the build manually:

1. Open your terminal.
2. Navigate to the plugin directory:
```powershell
cd $HOME\AppData\Local\nvim-data\plugged\peek.nvim

```


3. Run the build command directly:
```powershell
deno task build:fast

```



**3. Check for specific Windows errors**
If the manual build fails, check for these common Windows hurdles:

* **Execution Policy:** If PowerShell blocks the script, run `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser`.
* **Git Bash vs PowerShell:** If you are using Git Bash, the pathing might look different. It is generally safer to run the `deno` command in a standard PowerShell window.

### Update your `init.vim`

To ensure future updates don't break, double-check that your `Plug` line matches this (specifically using the `build:fast` task which is more reliable on Windows):

```vim
Plug 'toppair/peek.nvim', { 'do': 'deno task --quiet build:fast' }

```

Once the `deno task build:fast` command finishes successfully, you should see a `public` folder inside the plugin directory containing `main.bundle.js`. Restart Neovim, and the error should be gone.

```

```

--------------------------------

```json
{ "project_code": "VIM" , "links": "" , "location": "" , "fpoint": "" }
```
