-- Author: UlyssesZhan <ulysseszhan@gmail.com>
-- License: MIT
-- Homepage: https://github.com/UlyssesZh/bdanmaku

table.unpack = table.unpack or unpack -- 5.1 compatibility
local utils = require 'mp.utils'

local function load_conf_opts()
	local opts = {}
	local conf_path = mp.find_config_file('script-opts/bdanmaku.conf')
	if not conf_path then
		mp.msg.debug('no script-opts/bdanmaku.conf found')
		return opts
	end
	mp.msg.debug('loading script opts from ' .. conf_path)
	local file = io.open(conf_path, 'r')
	if not file then return opts end
	local count = 0
	for raw_line in file:lines() do
		local line = raw_line:match('^%s*(.-)%s*$')
		if line ~= '' and line:sub(1, 1) ~= '#' then
			local eq = line:find('=')
			if eq then
				local key = line:sub(1, eq - 1):match('^%s*(.-)%s*$')
				local value = line:sub(eq + 1):match('^%s*(.-)%s*$')
				if key and key ~= '' then
					opts[key] = value
					count = count + 1
				end
			end
		end
	end
	file:close()
	mp.msg.debug('loaded ' .. tostring(count) .. ' keys from conf')
	return opts
end

local CONF_OPTS = load_conf_opts()

local function get_opt(key, default)
	local val = mp.get_opt(key)
	if val == nil then
		val = CONF_OPTS[key]
	end
	local result = val or default
	mp.msg.debug('bdanmaku opt: ' .. key .. ' = ' .. tostring(result) .. ' (raw: ' .. tostring(val) .. ')')
	return result
end

local CURL = get_opt('curl_executable', 'curl')
local BILIASS = get_opt('biliass_executable', 'biliass')
local TMPDIR = get_opt('tmpdir', '/tmp/danmaku')
local CACHEDIR = (os.getenv('XDG_CACHE_HOME') or os.getenv('HOME')..'/.cache')..'/mpv/danmaku'
os.execute('mkdir -p '..CACHEDIR)
local BILIASS_OPTS = {}
for token in get_opt('biliass_options', '--fontsize 72 --alpha 0.5 --duration-marquee 20'):gmatch('[^%s]+') do
	BILIASS_OPTS[#BILIASS_OPTS + 1] = token
end

local TRANSLATE_ENABLED = get_opt('danmaku_translate', 'no') == 'yes'
local TRANSLATE_TARGET = get_opt('danmaku_translate_target', 'en')
local TRANSLATE_FONTSIZE_RATIO = get_opt('danmaku_translate_fontsize_ratio', '0.7')
local TRANSLATE_COLOR = get_opt('danmaku_translate_color', '&H00FFFF80')
local TRANSLATE_WORKERS = get_opt('danmaku_translate_workers', '4')
local TRANSLATE_MODE = get_opt('danmaku_translate_mode', 'offset')
local PYTHON3 = get_opt('python3_executable', 'python3')

local danmaku_track_id = nil
local xml_filename = nil
local active_ass_filename = nil
local has_unloaded = false
local is_translating = false
local danmaku_loaded = false

-- Detect the directory this script lives in, so we can find danmaku_translate.py
local function script_directory()
	local path = debug.getinfo(1, "S").source
	if path:sub(1, 1) == '@' then path = path:sub(2) end
	return path:match("^(.*[/\\])") or "./"
end

local TRANSLATE_SCRIPT = script_directory() .. 'danmaku_translate.py'

function execute(command)
	if _ENV then -- Lua 5.2
		local success, status, code = os.execute(command)
		return success and status == "exit" and code == 0
	else
		return os.execute(command) == 0
	end
end

function download_xml()
	local url = nil
	for track_i = 0, mp.get_property('track-list/count') - 1 do
		url = mp.get_property('track-list/'..track_i..'/external-filename')
		if url then
			url = url:match '%w+://comment.bilibili.com/.*%.xml$'
			if url then
				danmaku_track_id = track_i
				break
			end
		end
	end
	if not danmaku_track_id then
		mp.msg.debug('no XML danmaku found')
		return
	end
	danmaku_loaded = false
	if not execute('mkdir -p '..TMPDIR) then
		execute('powershell mkdir '..TMPDIR)
	end
	xml_filename = TMPDIR..'/'..mp.get_property('pid')..'.xml'
	local curl_args = {
		CURL, url,
		'--silent',
		'--output', xml_filename,
		'--compressed'
	}
	mp.msg.debug('curl_command: '..table.concat(curl_args, ' '))
	local curl_result = utils.subprocess({args = curl_args})
	if curl_result.status == 0 then
		mp.msg.debug('danmaku downloaded, will convert to ASS')
	else
		xml_filename = nil
		mp.msg.warn('downloading XML danmaku from '..url..' failed: '..curl_result.error)
	end
end

function run_translate(ass_filename, cache_filename)
	if not TRANSLATE_ENABLED then
		return ass_filename
	end
	if is_translating then
		mp.msg.debug('translation already in progress, skipping')
		return ass_filename
	end

	is_translating = true
	local translated_filename = ass_filename:gsub('%.ass$', '_translated.ass')
	local translate_args = {
		PYTHON3, TRANSLATE_SCRIPT,
		ass_filename,
		translated_filename,
		'--target', TRANSLATE_TARGET,
		'--ratio', TRANSLATE_FONTSIZE_RATIO,
		'--color', TRANSLATE_COLOR,
		'--workers', TRANSLATE_WORKERS,
		'--mode', TRANSLATE_MODE,
		'--cache', cache_filename,
	}
	mp.msg.info('running danmaku translate: '..table.concat(translate_args, ' '))
	local result = utils.subprocess({args = translate_args})
	is_translating = false

	if result.status == 0 then
		mp.msg.info('danmaku translation complete, using translated ASS')
		return translated_filename
	else
		mp.msg.warn('danmaku translation failed (status='..tostring(result.status)..'), using untranslated ASS')
		return ass_filename
	end
end

function replace_sub()
	local width, height, par = mp.get_osd_size()
	if width == 0 or height == 0 or not xml_filename or has_unloaded or danmaku_loaded then
		return
	end
	local resolution = width..'x'..height
	local ass_filename = TMPDIR..'/'..mp.get_property('pid')..'.ass'
	local biliass_args = {
		BILIASS, xml_filename,
		'--size', resolution,
		'--output', ass_filename,
		table.unpack(BILIASS_OPTS)
	}
	mp.msg.debug('biliass_command: '..table.concat(biliass_args, ' '))
	local biliass_result = utils.subprocess({args = biliass_args})
	if biliass_result.status == 0 then
		if TRANSLATE_ENABLED then
			mp.osd_message('Translating danmaku...', 999)
		end
		local cache_filename = CACHEDIR..'/translate_cache.json'
		ass_filename = run_translate(ass_filename, cache_filename)
		active_ass_filename = ass_filename
		local sid = mp.get_property('track-list/'..danmaku_track_id..'/id')
		mp.msg.debug('deleting original subtitle sid='..sid)
		mp.commandv('sub-remove', sid)
		mp.msg.debug('adding new subtitle')
		mp.commandv('sub-add', ass_filename, 'select', 'danmaku', 'danmaku')
		danmaku_loaded = true
		if TRANSLATE_ENABLED then
			mp.osd_message('Danmaku loaded', 3)
		end
		for track_i = 0, mp.get_property('track-list/count') - 1 do
			if mp.get_property('track-list/'..track_i..'/external-filename') == ass_filename then
				danmaku_track_id = track_i
				break
			end
		end
	else
		mp.msg.warn('converting XML danmaku from '..xml_filename..' to '..ass_filename..' failed: '..biliass_result.error)
	end
end

function unload_handler()
	mp.msg.debug('unload handler start')
	has_unloaded = true;
	local prefix = TMPDIR..'/'..mp.get_property('pid')
	os.remove(prefix..'.xml')
	os.remove(prefix..'.ass')
	os.remove(prefix..'_translated.ass')
end

function export_translated_ass()
	if not active_ass_filename then
		mp.osd_message('No translated danmaku loaded', 3)
		return
	end
	local export_dir = os.getenv('HOME')..'/danmaku_exports'
	os.execute('mkdir -p '..export_dir)
	local dest = export_dir..'/'..os.date('%Y%m%d_%H%M%S')..'_translated.ass'
	os.execute('cp '..active_ass_filename..' '..dest)
	mp.osd_message('Danmaku exported: '..dest, 3)
end

mp.register_event('file-loaded', download_xml)
mp.observe_property('osd-width', nil, replace_sub)
mp.observe_property('osd-height', nil, replace_sub)
mp.add_hook('on_unload', 50, unload_handler)
mp.add_key_binding('Ctrl+e', 'export-danmaku', export_translated_ass)
