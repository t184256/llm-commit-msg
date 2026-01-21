-- SPDX-FileCopyrightText: 2026 Alexander Sosedkin <monk@unboiled.info>
-- SPDX-License-Identifier: GPL-3.0

local M = {}

-- config

M.config = {
  bin = "llm-commit-msg",
  args = {},
  debug = false,
  auto = true,
}

local MARKER = "# ... llm-commit-msg ..."

-- logging helpers

local function dbg(msg)
  if M.config.debug then
    vim.notify("[llm-commit-msg] " .. msg)
  end
end

local function err(msg)
  vim.notify("[llm-commit-msg] " .. msg, vim.log.levels.ERROR)
end

-- position finding helpers

local function find_line(buf, predicate)
  local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
  for i, line in ipairs(lines) do
    if predicate(line) then
      return i - 1
    end
  end
  return nil
end

local function find_first_comment(buf)
  local found = find_line(buf, function(line) return line:match("^#") end)
  return found or vim.api.nvim_buf_line_count(buf)
end

local function find_marker(buf)
  return find_line(buf, function(line) return line == MARKER end)
end

-- text editing helpers

local function preserve_modified(buf, fn)
  local was_modified = vim.bo[buf].modified
  fn()
  vim.bo[buf].modified = was_modified
end

local function insert_marker_at_line(buf, at_line)
  preserve_modified(buf, function()
    vim.api.nvim_buf_set_lines(buf, at_line, at_line, false, { "", MARKER, "" })
  end)
end

local function replace_marker_line(buf, new_line)
  local mline = find_marker(buf)
  if not mline then return end
  preserve_modified(buf, function()
    vim.api.nvim_buf_set_lines(buf, mline, mline + 1, false, { new_line })
  end)
end

local function insert_before_marker(buf, text)
  local mline = find_marker(buf)
  if not mline then return end
  local lines = vim.split(text, "\n", { plain = true })
  local prev = vim.api.nvim_buf_get_lines(buf, mline - 1, mline, false)[1] or ""
  lines[1] = prev .. lines[1]
  preserve_modified(buf, function()
    vim.api.nvim_buf_set_lines(buf, mline - 1, mline, false, lines)
  end)
end

local function set_line_after_marker(buf, text)
  local mline = find_marker(buf)
  if not mline then return end
  preserve_modified(buf, function()
    vim.api.nvim_buf_set_lines(buf, mline + 1, mline + 2, false, { text })
  end)
end

local function remove_marker_line_and_line_after(buf)
  local mline = find_marker(buf)
  if not mline then return end
  preserve_modified(buf, function()
    vim.api.nvim_buf_set_lines(buf, mline + 0, mline + 2, false, {})
  end)
end

local function insert_after_marker(buf, lines)
  local mline = find_marker(buf)
  if not mline then return end
  preserve_modified(buf, function()
    vim.api.nvim_buf_set_lines(buf, mline + 1, mline + 1, false, lines)
  end)
end

-- handlers

local function mk_stdout_handler(buf)
  return function(read_err, data)
    if read_err then
      vim.schedule(function() err("stdout err: " .. tostring(read_err)) end)
      return
    end
    if not data then vim.schedule(function() dbg("stdout EOF") end) return end
    vim.schedule(function()
      if not vim.api.nvim_buf_is_valid(buf) then return end
      dbg("stdout chunk: " .. vim.inspect(data))
      insert_before_marker(buf, data)
    end)
  end
end

local function mk_stderr_handler(buf, stderr)
  return function(read_err, data)
    if not data then return end
    stderr.data = stderr.data .. data
    vim.schedule(function()
      if not vim.api.nvim_buf_is_valid(buf) then return end
      dbg("stderr chunk: " .. vim.inspect(data))
      local trimmed = stderr.data:gsub("\n+$", "")
      local last_line = trimmed:match("[^\n]*$") or ""
      set_line_after_marker(buf, "# " .. last_line)
    end)
  end
end

local function mk_exit_handler(buf, pipes, stderr)
  return function(code)
    pipes.stdout:close()
    pipes.stderr:close()
    vim.schedule(function()
      if not vim.api.nvim_buf_is_valid(buf) then return end
      dbg("process exited with code=" .. code)
      if code ~= 0 and stderr.data ~= "" then
        replace_marker_line(buf, "# llm-commit-msg exited with " .. code)
        local stderr_lines = {}
        for line in stderr.data:gmatch("[^\n]+") do
          local prefixed = line:match("^#") and line or ("# " .. line)
          table.insert(stderr_lines, prefixed)
        end
        insert_after_marker(buf, stderr_lines)
      else
        remove_marker_line_and_line_after(buf)
      end
    end)
  end
end

-- main function

function M.generate(buf)
  buf = buf or vim.api.nvim_get_current_buf()
  if not vim.api.nvim_buf_is_valid(buf) then return end

  local insert_pos = find_first_comment(buf)
  dbg("inserting marker at line " .. insert_pos)
  insert_marker_at_line(buf, insert_pos)

  local pipes = {
    stdout = vim.uv.new_pipe(),
    stderr = vim.uv.new_pipe(),
  }
  local stderr = { data = "" }

  local args = vim.list_extend(
    { "generate", "--commented-out" }, M.config.args
  )
  dbg("spawning: " .. M.config.bin .. " " .. table.concat(args, " "))

  local handle, pid = vim.uv.spawn(M.config.bin, {
    args = args,
    stdio = { nil, pipes.stdout, pipes.stderr },
  }, mk_exit_handler(buf, pipes, stderr))

  if not handle then
    err("failed to spawn")
    replace_marker_line(buf, "# llm-commit-msg: failed to spawn")
    return
  end
  dbg("spawned pid=" .. (pid or "nil"))

  pipes.stdout:read_start(mk_stdout_handler(buf))
  pipes.stderr:read_start(mk_stderr_handler(buf, stderr))
end

-- plugin initialization

function M.setup(opts)
  M.config = vim.tbl_deep_extend("force", M.config, opts or {})
  vim.api.nvim_create_user_command("LlmCommitMsg", function()
    M.generate()
  end, {})
  if M.config.auto then
    vim.api.nvim_create_autocmd("FileType", {
      pattern = "gitcommit",
      group = vim.api.nvim_create_augroup("llm-commit-msg", { clear = true }),
      callback = function(ev)
        vim.defer_fn(function() M.generate(ev.buf) end, 0)
      end,
    })
  end
end

return M
