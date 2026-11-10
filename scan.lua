-- Recorre la config de Hyprland con un `hl` simulado y emite cada bind/unbind
-- con su archivo y línea de origen. Una línea por evento, campos separados por \t:
--   B  keys  desc  kind  arg  file  line  locked  repeating
--   U  keys  -     -     -    file  line
--   E  mensaje de error
local function clean(s)
  return (tostring(s or ""):gsub("[\t\r\n]", " "))
end

local function origin()
  for level = 3, 40 do
    local info = debug.getinfo(level, "Sl")
    if not info then break end
    local src = info.source or ""
    if src:sub(1, 1) == "@" and not src:match("/default/hypr/helpers%.lua$") then
      return src:sub(2), info.currentline or 0
    end
  end
  return "?", 0
end

local function lua_literal(v)
  local t = type(v)
  if t == "string" then return string.format("%q", v)
  elseif t == "number" or t == "boolean" then return tostring(v)
  elseif t == "table" then
    local parts, keys = {}, {}
    for i = 1, #v do parts[#parts + 1] = lua_literal(v[i]) end
    for k in pairs(v) do
      if not (type(k) == "number" and k >= 1 and k <= #v) then keys[#keys + 1] = k end
    end
    table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
    for _, k in ipairs(keys) do parts[#parts + 1] = tostring(k) .. " = " .. lua_literal(v[k]) end
    return "{ " .. table.concat(parts, ", ") .. " }"
  end
  return "nil"
end

local function dsp_proxy(path)
  return setmetatable({ path = path }, {
    __index = function(self, k) return dsp_proxy(self.path .. "." .. tostring(k)) end,
    __call = function(self, ...)
      local args = {}
      for i = 1, select("#", ...) do args[i] = lua_literal(select(i, ...)) end
      local first = ...
      if self.path == "hl.dsp.exec_cmd" and type(first) == "string" then
        return { __dsp = true, kind = "exec", arg = first }
      end
      return { __dsp = true, kind = "lua", arg = self.path:gsub("^hl%.dsp%.", "") .. "(" .. table.concat(args, ", ") .. ")" }
    end,
  })
end

local noop
noop = setmetatable({}, { __index = function() return noop end, __call = function() return noop end })

hl = setmetatable({
  dsp = dsp_proxy("hl.dsp"),
  bind = function(keys, d, opts)
    opts = opts or {}
    local kind, arg = "lua", "función"
    if type(d) == "table" and d.__dsp then kind, arg = d.kind, d.arg
    elseif type(d) == "string" then kind, arg = "exec", d end
    local file, line = origin()
    print(table.concat({ "B", clean(keys), clean(opts.description), kind, clean(arg), file, tostring(line),
      tostring(opts.locked == true), tostring(opts.repeating == true) }, "\t"))
    return noop
  end,
  unbind = function(keys)
    local file, line = origin()
    print(table.concat({ "U", clean(keys), "", "", "", file, tostring(line), "", "" }, "\t"))
    return noop
  end,
  get_config = function() return nil end,
}, { __index = function() return noop end })

local config = arg[1] or (os.getenv("HOME") .. "/.config/hypr/hyprland.lua")
local ok, err = pcall(dofile, config)
if not ok then print("E\t" .. clean(err)) end
