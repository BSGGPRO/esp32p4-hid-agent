/* 中英双语：不碰 app.js 的逻辑，等它把字写进 DOM 之后再翻译。
 *
 * 为什么这么做：面板文字一半在 index.html，一半在 app.js 里每秒重写。要改 80 处调用点
 * 才能国际化，风险大还容易改坏轮询逻辑。这里改成"渲染后翻译"：一张双语词表 + 少量正则，
 * 用 MutationObserver 在每次刷新后补一次翻译。app.js 完全不用动。
 *
 * 加词条：往 PAIRS 里加 [中文, English]；带数字的句子往 RULES 里加。
 * 没收录的句子会原样保留（中文界面不受影响），所以漏词不会坏功能。
 */
(function () {
  'use strict';

  var PAIRS = [
    // ---- 页头 / 页脚 ----
    ['P4 HID Agent · 网页仪表盘', 'P4 HID Agent · Dashboard'],
    ['ESP32-P4 板载状态', 'ESP32-P4 on-board status'],
    ['板子连接中…', 'Connecting to board…'],
    ['立即刷新', 'Refresh now'],
    ['立即刷新板子数据', 'Refresh board data now'],
    ['板子在线', 'Board online'],
    ['板子离线', 'Board offline'],
    ['板子被占用', 'Board busy'],
    ['板子透传中', 'Board passthrough'],
    ['板子数据超时', 'Board data stale'],
    ['等待数据', 'Waiting for data'],
    ['最后更新', 'Last update'],
    ['离线', 'offline'],
    ['固件无 stats', 'Firmware has no stats'],
    ['正在等待板子数据…', 'Waiting for board data…'],
    [
      '页面由 PC 上的 hub 托管，每秒读取同源 api/board；hub 通过串口 COM7 采集板子统计。',
      'Served by the PC hub; polls the same-origin api/board every second. The hub collects board stats over the COM7 serial port.'
    ],
    ['板子暂无数据', 'No board data yet'],
    // ---- 指标 ----
    ['板子', 'Board'],
    ['运行时长', 'Uptime'],
    ['CPU 主频', 'CPU clock'],
    ['HID 枚举', 'HID enumerated'],
    ['采样窗口', 'Sample window'],
    ['板子温度', 'Board temp'],
    ['固件可选字段 temp_c / temp_warn；缺失时显示 —', 'Optional firmware fields temp_c / temp_warn; shows — when absent'],
    ['双核占用', 'Dual-core load'],
    ['最近 120 秒', 'last 120 s'],
    ['内存', 'Memory'],
    ['Heap 空闲', 'Heap free'],
    ['Heap 历史最低', 'Heap low-water'],
    ['Heap 总量', 'Heap total'],
    ['空闲占比', 'Free ratio'],
    ['PSRAM 空闲', 'PSRAM free'],
    ['PSRAM 总量', 'PSRAM total'],
    ['TF 卡', 'TF card'],
    ['类型', 'Type'],
    ['容量', 'Capacity'],
    ['可用', 'Free'],
    ['写速度', 'Write speed'],
    ['读速度', 'Read speed'],
    ['未插卡', 'No card'],
    ['已挂载', 'Mounted'],
    ['挂载失败', 'Mount failed'],
    ['动态调频（DFS）', 'Dynamic frequency scaling (DFS)'],
    ['稳定模式 · 固定 360MHz', 'Stable · fixed 360MHz'],
    ['省电模式 · 动态调频', 'Power-save · DFS'],
    ['开启：空闲时自动降频省电；关闭：固定 360MHz（稳定优先）',
      'On: drop frequency when idle to save power. Off: fixed 360MHz (stability first).'],
    ['默认关闭动态调频：卡/USB 更稳；开了可能省电但偶发不稳。',
      'DFS is off by default: steadier card/USB. Turning it on saves power but can get flaky.'],
    ['调度器', 'Governor'],
    ['升 / 降频', 'Boosts / drops'],
    ['当前频率', 'Current clock'],
    ['频率范围', 'Clock range'],
    ['空闲实测', 'Idle (measured)'],
    ['调度自检', 'Governor check'],
    ['智能调度', 'adaptive'],
    ['固定 360', 'fixed 360'],
    ['固件未上报', 'not reported'],
    ['已开启', 'on'],
    ['已关闭', 'off'],
    ['当前固件不支持切换', 'Current firmware cannot switch'],
    ['固件升级到带 pm 字段的版本后，这个开关才能用。',
      'This switch works once the firmware reports the pm fields.'],
    ['空闲时自动降频省电；若出现鼠标断续或卡异常，请关掉。',
      'Drops frequency when idle to save power; turn it off if the mouse stutters or the card misbehaves.'],
    ['固定 360MHz，SD 卡与 USB 最稳；发热实测约 24°C，无需担心。',
      'Fixed 360MHz keeps SD and USB steadiest; measured about 24°C, nothing to worry about.'],
    ['已降频 ✓', 'clock dropped ✓'],
    ['未见降频 ⚠', 'no drop seen ⚠'],
    ['满频 ✓', 'full clock ✓'],
    ['未满频 ⚠', 'not full clock ⚠'],
    ['网络', 'Network'],
    ['链路速率', 'Link rate'],
    ['网关', 'Gateway'],
    ['已连接', 'up'],
    ['未连接', 'down'],
    ['是', 'yes'],
    ['否', 'no'],
    ['UART 计数', 'UART counters'],
    ['收帧正常', 'RX ok'],
    ['收帧异常', 'RX bad'],
    ['发帧', 'TX'],
    ['FreeRTOS 任务', 'FreeRTOS tasks'],
    ['任务', 'Task'],
    ['优先级', 'Prio'],
    ['状态', 'State'],
    ['栈余量 HWM', 'Stack HWM'],
    ['暂无任务数据', 'No task data'],
    ['过热 ⚠', 'overheat ⚠'],
    ['⚠ 过热', '⚠ hot'],
    ['已枚举', 'enumerated'],
    ['未枚举', 'not enumerated'],
    ['刚刚', 'just now'],
    ['透传中', 'passthrough'],
    ['切换中…', 'switching…'],
    // ---- 页脚 ----
    ['板子：同源 api/board（hub 经 COM7 串口采集，每秒刷新）',
      'Board: same-origin api/board (hub reads COM7, refreshed every second)'],
    ['每秒自动刷新', 'auto-refresh every second'],
    ['断线自动重连，数据超时 3.2 s 自动标灰',
      'Auto-reconnects; greys out after 3.2 s without data'],
    // ---- 错误与提示 ----
    ['响应不是 JSON 对象', 'Response is not a JSON object'],
    ['未知错误', 'unknown error'],
    ['未收到错误信息', 'no error detail'],
    ['hub 打不开串口 COM7（板子没插 / 串口被占用 / COM 号变了）。hub 会自动重试。',
      'hub cannot open COM7 (board unplugged / port busy / COM number changed). hub keeps retrying.'],
    ['板子锁被其他程序占用（可能是 fw-core 烧录或 draw 在画图），hub 会等锁释放后自动接管。',
      'Board lock held by another program (flashing or drawing); hub takes over once released.'],
    ['串口已连上，但固件没有返回 stats 数据（固件可能还没升级到 v1.2）。hub 会继续重试。',
      'Serial is up but the firmware returns no stats (may be older than v1.2). hub keeps retrying.'],
    ['板子串口正被 TCP 透传占用（brain.py），stats 暂停刷新；',
      'The serial port is held by TCP passthrough (brain.py); stats are paused. '],
    ['透传结束后会自动恢复（数据超过 3.2 s 未更新会标灰）。',
      'It resumes when passthrough ends (data greys out after 3.2 s).'],
    // 上面两句会被拼成一条文本，整句单独再收一次
    ['板子串口正被 TCP 透传占用（brain.py），stats 暂停刷新；透传结束后会自动恢复（数据超过 3.2 s 未更新会标灰）。',
      'The serial port is held by TCP passthrough (brain.py); stats are paused. It resumes when passthrough ends (data greys out after 3.2 s).'],
    ['⟳ 立即刷新', '⟳ Refresh now'],
    ['无任务数据', 'no task data']
  ];

  // 短词兜底：长句里夹的时间词（"透传中 · 最后更新 刚刚"）靠它补齐
  var WORDS = [
    ['刚刚', 'just now'],
    [' 前', ' ago'],
    [' 天 ', ' d ']
  ];

  // 带数字/变量的句子：[中文正则, 英文模板, 英文正则, 中文模板]
  var RULES = [
    [/^(\d+) 个任务$/, '$1 tasks', /^(\d+) tasks$/, '$1 个任务'],
    [/^(\d+) 天 (.+)$/, '$1 d $2', /^(\d+) d (.+)$/, '$1 天 $2'],
    [/^(刚提交|刚刚)$/, 'just now', /^just now$/, '刚刚'],
    [/^([\d.]+ s|[\d.]+ min|[\d.]+ h) 前$/, '$1 ago', /^([\d.]+ s|[\d.]+ min|[\d.]+ h) ago$/, '$1 前'],
    [/^统计窗口 (\d+) ms$/, 'window $1 ms', /^window (\d+) ms$/, '统计窗口 $1 ms'],
    [/^更新于 (.+)$/, 'updated $1', /^updated (.+)$/, '更新于 $1'],
    [/^最后更新 (.+)$/, 'last update $1', /^last update (.+)$/, '最后更新 $1'],
    [/^透传中 · 最后更新 (.+)$/, 'passthrough · last update $1',
      /^passthrough · last update (.+)$/, '透传中 · 最后更新 $1'],
    [/^升 (\d+) \/ 降 (\d+)$/, 'up $1 / down $2', /^up (\d+) \/ down (\d+)$/, '升 $1 / 降 $2'],
    [/^升 (\d+) \/ 降 (\d+) · 上次：(.+)$/, 'up $1 / down $2 · last: $3',
      /^up (\d+) \/ down (\d+) · last: (.+)$/, '升 $1 / 降 $2 · 上次：$3'],
    [/^(\d+) °C ⚠ 过热$/, '$1 °C ⚠ hot', /^([\d.]+) °C ⚠ hot$/, '$1 °C ⚠ 过热'],
    [/^挂载失败：(.+)$/, 'mount failed: $1', /^mount failed: (.+)$/, '挂载失败：$1'],
    [/^hub 暂时不可用：(.+)$/, 'hub unavailable: $1', /^hub unavailable: (.+)$/, 'hub 暂时不可用：$1'],
    [/^切换失败：(.+)$/, 'switch failed: $1', /^switch failed: (.+)$/, '切换失败：$1'],
    [/^已切换到省电模式（动态调频）。$/, 'Switched to power-save (DFS).',
      /^Switched to power-save \(DFS\)\.$/, '已切换到省电模式（动态调频）。'],
    [/^已切换到稳定模式（固定 360MHz）。$/, 'Switched to stable (fixed 360MHz).',
      /^Switched to stable \(fixed 360MHz\)\.$/, '已切换到稳定模式（固定 360MHz）。'],
    [/^板子数据已 (\d+) s 未更新，正在自动重连…（(.+)）$/,
      'No board data for $1 s, reconnecting… ($2)',
      /^No board data for (\d+) s, reconnecting… \((.+)\)$/,
      '板子数据已 $1 s 未更新，正在自动重连…（$2）']
  ];

  var Z2E = Object.create(null), E2Z = Object.create(null);
  PAIRS.forEach(function (p) { Z2E[p[0]] = p[1]; E2Z[p[1]] = p[0]; });

  var lang = 'zh';
  var applying = false;

  function words(core, dir) {
    // dir 0: 中→英；dir 1: 英→中。整句没收录时靠它把尾巴上的时间词补上。
    for (var i = 0; i < WORDS.length; i++) {
      var from = dir === 0 ? WORDS[i][0] : WORDS[i][1];
      var to = dir === 0 ? WORDS[i][1] : WORDS[i][0];
      if (core.indexOf(from) !== -1) core = core.split(from).join(to);
    }
    return core;
  }

  function translate(text) {
    var lead = text.match(/^\s*/)[0];
    var tail = text.match(/\s*$/)[0];
    var core = text.trim();
    if (!core) return null;
    var out = null;
    var dir = lang === 'en' ? 0 : 1;
    if (dir === 0 && Z2E[core]) out = Z2E[core];
    if (dir === 1 && E2Z[core]) out = E2Z[core];
    if (out === null) {
      for (var i = 0; i < RULES.length; i++) {
        var re = dir === 0 ? RULES[i][0] : RULES[i][2];
        if (re.test(core)) {
          out = dir === 0 ? core.replace(re, RULES[i][1]) : core.replace(re, RULES[i][3]);
          break;
        }
      }
    }
    if (out === null) out = core;                 // 没收录就原样，别把界面搞乱
    out = words(out, dir);
    var res = lead + out + tail;
    return res === text ? null : res;             // 没变化就别碰 DOM
  }

  var SKIP = { SCRIPT: 1, STYLE: 1, CODE: 1, TEXTAREA: 1, INPUT: 1 };

  function walkText(node) {
    for (var c = node.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) {
        var t = translate(c.nodeValue);
        if (t !== null && t !== c.nodeValue) c.nodeValue = t;
      } else if (c.nodeType === 1 && !SKIP[c.tagName]) {
        var tag = c.tagName;
        if (c.hasAttribute && (c.hasAttribute('title') || c.hasAttribute('placeholder'))) {
          ['title', 'placeholder'].forEach(function (a) {
            var v = c.getAttribute(a);
            if (!v) return;
            var tv = translate(v);
            if (tv !== null && tv !== v) c.setAttribute(a, tv);
          });
        }
        if (tag !== 'CODE') walkText(c);
      }
    }
  }

  function apply(force) {
    if (applying) return;
    applying = true;
    try {
      if (document.title) {
        var dt = translate(document.title);
        if (dt !== null && dt !== document.title) document.title = dt;
      }
      walkText(document.body);
      document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
    } finally {
      applying = false;
    }
    var sel = document.getElementById('lang-select');
    if (sel && sel.value !== lang) sel.value = lang;
  }

  function setLang(next) {
    lang = next === 'en' ? 'en' : 'zh';
    try { localStorage.setItem('p4.lang', lang); } catch (e) { /* 隐私模式 */ }
    apply();
  }

  function init() {
    var saved = null;
    try { saved = localStorage.getItem('p4.lang'); } catch (e) { /* 隐私模式 */ }
    // ?lang=en / ?lang=zh 可直接指定（方便贴链接、也方便排查），并记住选择
    var q = (location.search.match(/[?&]lang=(zh|en)\b/) || [])[1];
    lang = q || saved || ((navigator.language || 'zh').toLowerCase().indexOf('zh') === 0 ? 'zh' : 'en');
    if (q) { try { localStorage.setItem('p4.lang', lang); } catch (e) { /* 隐私模式 */ } }
    var sel = document.getElementById('lang-select');
    if (sel) {
      sel.value = lang;
      sel.addEventListener('change', function () { setLang(sel.value); });
    }
    apply();
    // app.js 每秒重写文字：必须在同一个微任务里补翻完。用定时器延后哪怕 60ms，
    // 浏览器就会先画出中文、再画一次英文——看起来就是"选项在中英之间闪"。
    // MutationObserver 的回调在同一帧渲染前执行，所以这里不加任何延时。
    new MutationObserver(function () { apply(); })
      .observe(document.body, { subtree: true, childList: true, characterData: true });
  }

  window.P4I18N = { setLang: setLang, apply: apply, get lang() { return lang; } };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
