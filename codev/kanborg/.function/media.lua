local function notify(msg, level)
    vim.notify('InsertMedia: ' .. msg, level or vim.log.levels.INFO)
end

local function abs(path)
    local p = vim.fn.fnamemodify(vim.fn.expand(path), ':p')
    if #p > 1 then
        p = p:gsub('[/\\]+$', '')
    end
    return p
end

local function split_path(path)
    local parts = {}
    for part in path:gmatch('[^/]+') do
        if part == '..' then
            table.remove(parts)
        elseif part ~= '.' then
            table.insert(parts, part)
        end
    end
    return parts
end

local function relpath(from_dir, to_path)
    local from = split_path(abs(from_dir))
    local to = split_path(abs(to_path))
    local i = 1
    while from[i] and to[i] and from[i] == to[i] do
        i = i + 1
    end
    local out = {}
    for _ = i, #from do
        table.insert(out, '..')
    end
    for j = i, #to do
        table.insert(out, to[j])
    end
    return table.concat(out, '/')
end

local function card_context()
    local buf = vim.api.nvim_get_current_buf()
    local first = vim.api.nvim_buf_get_lines(buf, 0, 1, false)[1] or ''
    local offset = first:match('^>>>> Viewing for ') and 1 or 0
    local card = vim.b[buf].kanborg_card
    if not card or card == '' then
        local name = vim.api.nvim_buf_get_name(buf)
        if name:match('%.md$') then
            local cycle = vim.g.cycle_dir
            if cycle and cycle ~= '' then
                local prefix = abs(cycle) .. '/'
                local absname = abs(name)
                if absname:sub(1, #prefix) == prefix
                    and vim.fn.match(absname, '\\v/(BACKLOG|TODO|HOLD|WIP|DONE)/') >= 0 then
                    card = name
                end
            end
        end
    end
    return card, offset
end

local image_ext = {
    png = true, jpg = true, jpeg = true, gif = true, svg = true,
    webp = true, bmp = true, tif = true, tiff = true, ico = true,
    avif = true, heic = true,
}

local function is_image(path)
    return image_ext[vim.fn.fnamemodify(path, ':e'):lower()] == true
end

local function unique_path(dir, name)
    local candidate = dir .. '/' .. name
    if vim.fn.filereadable(candidate) == 0 and vim.fn.isdirectory(candidate) == 0 then
        return candidate
    end
    local base = vim.fn.fnamemodify(name, ':r')
    local ext = vim.fn.fnamemodify(name, ':e')
    local suffix = ext ~= '' and ('.' .. ext) or ''
    local i = 1
    while true do
        candidate = dir .. '/' .. base .. '-' .. i .. suffix
        if vim.fn.filereadable(candidate) == 0 and vim.fn.isdirectory(candidate) == 0 then
            return candidate
        end
        i = i + 1
    end
end

local function insert_media(src)
    local card, offset = card_context()
    if not card then
        notify('cursor is not inside a card view or card buffer', vim.log.levels.WARN)
        return
    end
    if vim.fn.filereadable(src) == 0 then
        notify('cannot read ' .. src, vim.log.levels.ERROR)
        return
    end

    local card_abs = abs(card)
    local card_dir = vim.fn.fnamemodify(card_abs, ':h')
    local card_name = vim.fn.fnamemodify(card_abs, ':t:r')
    local cycle_dir = abs(vim.g.cycle_dir)
    local media_dir = cycle_dir .. '/.media/' .. card_name
    vim.fn.mkdir(media_dir, 'p')

    local dest = unique_path(media_dir, vim.fn.fnamemodify(src, ':t'))
    local ok, err = vim.uv.fs_copyfile(src, dest)
    if not ok then
        notify('could not copy media: ' .. tostring(err), vim.log.levels.ERROR)
        return
    end

    local link = relpath(card_dir, dest)
    local label = vim.fn.fnamemodify(dest, ':t')
    local text = is_image(dest)
        and ('![' .. label .. '](' .. link .. ')')
        or ('[' .. label .. '](' .. link .. ')')

    if offset == 1 then
        local lines = vim.fn.readfile(card)
        local row = vim.api.nvim_win_get_cursor(0)[1] - offset
        row = math.max(1, math.min(row, math.max(#lines, 1)))
        if #lines == 0 then
            lines = { text }
            row = 1
        else
            table.insert(lines, row + 1, text)
        end
        vim.fn.writefile(lines, card)
        vim.fn.ViewCard(card)
        vim.api.nvim_win_set_cursor(0, {
            math.min(row + 1 + offset, vim.api.nvim_buf_line_count(0)),
            0,
        })
    else
        local row = vim.api.nvim_win_get_cursor(0)[1]
        vim.api.nvim_buf_set_lines(0, row, row, false, { text })
        vim.api.nvim_win_set_cursor(0, { row + 1, 0 })
    end

    notify('inserted ' .. link)
end

local function list_dir(dir, show_hidden)
    local entries = {
        {
            name = '../',
            path = vim.fn.fnamemodify(dir, ':h'),
            is_dir = true,
        },
    }
    local ok, names = pcall(vim.fn.readdir, dir)
    if not ok then
        return entries
    end
    table.sort(names, function(a, b)
        return a:lower() < b:lower()
    end)
    for _, name in ipairs(names) do
        if show_hidden or name:sub(1, 1) ~= '.' then
            local path = dir .. '/' .. name
            table.insert(entries, {
                name = name,
                path = path,
                is_dir = vim.fn.isdirectory(path) == 1,
            })
        end
    end
    return entries
end

local media_ns = vim.api.nvim_create_namespace('kanborg_media')

local function render(state)
    local lines = {}
    for _, entry in ipairs(state.entries) do
        local suffix = entry.is_dir and entry.name ~= '../' and '/' or ''
        table.insert(lines, entry.name .. suffix)
    end
    if #lines == 0 then
        lines = { '(empty)' }
    end

    vim.bo[state.buf].modifiable = true
    vim.api.nvim_buf_set_lines(state.buf, 0, -1, false, lines)
    vim.bo[state.buf].modifiable = false

    vim.api.nvim_buf_clear_namespace(state.buf, media_ns, 0, -1)
    for i, entry in ipairs(state.entries) do
        if entry.is_dir then
            vim.api.nvim_buf_add_highlight(state.buf, media_ns, 'Directory', i - 1, 0, -1)
        end
    end

    vim.api.nvim_win_set_config(state.win, {
        title = state.dir,
        title_pos = 'center',
    })
    vim.api.nvim_win_set_cursor(state.win, { 1, 0 })
end

local function open_browser(start_dir, on_select)
    start_dir = abs(start_dir)
    if vim.fn.isdirectory(start_dir) == 0 then
        start_dir = abs('~')
    end

    local origin = vim.api.nvim_get_current_win()
    local width = math.min(math.max(math.floor(vim.o.columns * 0.6), 40), vim.o.columns - 4)
    local height = math.min(math.max(math.floor(vim.o.lines * 0.6), 10), vim.o.lines - 4)
    local row = math.max(0, math.floor((vim.o.lines - height) / 2) - 1)
    local col = math.max(0, math.floor((vim.o.columns - width) / 2))

    local buf = vim.api.nvim_create_buf(false, true)
    local win = vim.api.nvim_open_win(buf, true, {
        relative = 'editor',
        width = width,
        height = height,
        row = row,
        col = col,
        style = 'minimal',
        border = 'rounded',
        title = start_dir,
        title_pos = 'center',
    })

    local state = {
        buf = buf,
        win = win,
        dir = start_dir,
        show_hidden = false,
        entries = {},
        on_select = on_select,
        origin = origin,
    }

    local function close()
        if vim.api.nvim_win_is_valid(win) then
            vim.api.nvim_win_close(win, true)
        end
        if vim.api.nvim_buf_is_valid(buf) then
            vim.api.nvim_buf_delete(buf, { force = true })
        end
        if vim.api.nvim_win_is_valid(origin) then
            vim.api.nvim_set_current_win(origin)
        end
    end

    local navigate

    local function choose()
        local row_now = vim.api.nvim_win_get_cursor(win)[1]
        local entry = state.entries[row_now]
        if not entry then
            return
        end
        if entry.is_dir then
            navigate(entry.path)
        else
            local file = entry.path
            close()
            vim.schedule(function()
                state.on_select(file)
            end)
        end
    end

    local function toggle_hidden()
        state.show_hidden = not state.show_hidden
        state.entries = list_dir(state.dir, state.show_hidden)
        render(state)
    end

    local function goto_path()
        local input = vim.fn.input('Go to: ', state.dir .. '/', 'file')
        if input == '' then
            return
        end
        local target = abs(input)
        if vim.fn.isdirectory(target) == 1 then
            navigate(target)
        elseif vim.fn.filereadable(target) == 1 then
            close()
            vim.schedule(function()
                state.on_select(target)
            end)
        else
            notify('no such file or directory: ' .. target, vim.log.levels.WARN)
        end
    end

    navigate = function(dir)
        dir = abs(dir)
        if vim.fn.isdirectory(dir) == 0 then
            notify('not a directory: ' .. dir, vim.log.levels.WARN)
            return
        end
        state.dir = dir
        state.entries = list_dir(dir, state.show_hidden)
        render(state)
    end

    local opts = { buffer = buf, nowait = true, silent = true }
    vim.keymap.set('n', '<CR>', choose, opts)
    vim.keymap.set('n', 'l', choose, opts)
    vim.keymap.set('n', 'h', function()
        navigate(vim.fn.fnamemodify(state.dir, ':h'))
    end, opts)
    vim.keymap.set('n', '<BS>', function()
        navigate(vim.fn.fnamemodify(state.dir, ':h'))
    end, opts)
    vim.keymap.set('n', '.', toggle_hidden, opts)
    vim.keymap.set('n', 'g', goto_path, opts)
    vim.keymap.set('n', 'q', close, opts)
    vim.keymap.set('n', '<Esc>', close, opts)
    vim.keymap.set('n', '<C-c>', close, opts)

    vim.wo[win].cursorline = true
    vim.wo[win].wrap = false
    vim.wo[win].winhl = 'CursorLine:Visual,Directory:Directory'
    vim.bo[buf].buftype = 'nofile'
    vim.bo[buf].bufhidden = 'wipe'
    vim.bo[buf].swapfile = false
    vim.bo[buf].modifiable = false

    navigate(start_dir)
end

vim.api.nvim_create_user_command('InsertMedia', function(args)
    local start = args.args ~= '' and abs(args.args) or abs('~')
    if args.args ~= '' and vim.fn.filereadable(start) == 1 then
        insert_media(start)
        return
    end
    if vim.fn.isdirectory(start) == 0 then
        start = abs('~')
    end
    open_browser(start, insert_media)
end, { nargs = '?', complete = 'file', desc = 'Insert media into the current card' })
