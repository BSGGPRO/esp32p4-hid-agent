'use strict';

/* ============================================================
 * P4 HID Agent 网页仪表盘
 *
 * 两块数据源（都与页面同源，由 PC 上的 pc/hub.py 提供）：
 *   1) 板子：GET api/board —— hub 独占 COM7，每秒发 stats 命令并缓存结果
 *   2) 本机：GET api/pc    —— hub 采集 CPU/内存/GPU/进程/NPU
 *
 * 设计约束：
 *   - 纯原生 JS，无构建工具、无外网 CDN；
 *   - 每秒轮询，失败自动重连；
 *   - 超过 3.2 s 没有新数据 → 面板标灰 + 状态胶囊变「超时」；
 *   - 板子离线/被占用/固件无 stats 时显示对应离线态，不假装有数据；
 *   - 手机竖屏：CSS 流式布局（见 style.css 媒体查询）。
 * ============================================================ */

(function () {
  'use strict';

  // ------------------------------------------------------------------
  // 常量
  // ------------------------------------------------------------------
  var API_BOARD = 'api/board';
  var API_PC = 'api/pc';
  var BOARD_POLL_MS = 1000;        // 板子数据轮询周期
  var PC_POLL_MS = 1000;           // 本机数据轮询周期
  var BOARD_TIMEOUT_MS = 2000;     // 单次请求超时（hub 自己的 stats 缓存最多 1 s 旧）
  var PC_TIMEOUT_MS = 2500;
  var STALE_MS = 3200;             // 超过这个时间没新数据 = 数据超时（标灰）
  var HISTORY_LEN = 120;           // 双核曲线保留最近 120 个采样点
  var API_CMD = 'api/cmd';         // POST：网页按钮 -> hub -> 板子
  var pmSending = false;           // 动态调频开关切换中（避免被轮询回写覆盖）

  var BOARD_ERR_TEXT = {
    board_offline: '板子离线',
    board_busy: '板子被占用',
    board_no_stats: '固件无 stats'
  };

  // ------------------------------------------------------------------
  // DOM 小工具
  // ------------------------------------------------------------------
  function $(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    var el = $(id);
    if (el) {
      el.textContent = text;
    }
  }

  function toggleHidden(el, hidden) {
    if (el) {
      el.classList.toggle('hidden', !!hidden);
    }
  }

  function toggleClass(el, cls, on) {
    if (el) {
      el.classList.toggle(cls, !!on);
    }
  }

  // ------------------------------------------------------------------
  // 格式化
  // ------------------------------------------------------------------
  function num(value) {
    if (value === null || value === undefined || value === '') {
      return null;
    }
    var n = typeof value === 'number' ? value : parseFloat(value);
    return isFinite(n) ? n : null;
  }

  function fmtInt(value) {
    var n = num(value);
    if (n === null) {
      return '—';
    }
    return Math.round(n).toLocaleString('zh-CN');
  }

  function fmtPct(value, digits) {
    var n = num(value);
    if (n === null) {
      return '—';
    }
    return n.toFixed(digits === undefined ? 1 : digits) + '%';
  }

  function fmtBytes(bytes) {
    var n = num(bytes);
    if (n === null || n < 0) {
      return '—';
    }
    if (n >= 1024 * 1024) {
      return (n / (1024 * 1024)).toFixed(2) + ' MB';
    }
    if (n >= 1024) {
      return (n / 1024).toFixed(1) + ' KB';
    }
    return n.toFixed(0) + ' B';
  }

  function fmtMb(mb) {
    var n = num(mb);
    if (n === null) {
      return '—';
    }
    if (n >= 1024) {
      return (n / 1024).toFixed(n >= 10240 ? 1 : 2) + ' GB';
    }
    return n.toFixed(n >= 100 ? 0 : 1) + ' MB';
  }

  function fmtKbps(kbps) {
    var n = num(kbps);
    if (n === null || n <= 0) {
      return '—';
    }
    if (n >= 1024) {
      return (n / 1024).toFixed(1) + ' MB/s';
    }
    return n.toFixed(0) + ' KB/s';
  }

  function fmtDuration(seconds) {
    var s = num(seconds);
    if (s === null || s < 0) {
      return '—';
    }
    s = Math.floor(s);
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    var hms = pad2(h) + ':' + pad2(m) + ':' + pad2(sec);
    return d > 0 ? d + ' 天 ' + hms : hms;
  }

  function fmtDurationMs(ms) {
    var n = num(ms);
    if (n === null) {
      return '—';
    }
    return fmtDuration(n / 1000);
  }

  function pad2(n) {
    return (n < 10 ? '0' : '') + n;
  }

  function fmtAgo(ms) {
    var n = num(ms);
    if (n === null || n < 0) {
      return '—';
    }
    if (n < 1000) {
      return '刚刚';
    }
    if (n < 60000) {
      return (n / 1000).toFixed(1) + ' s 前';
    }
    return Math.floor(n / 60000) + ' min 前';
  }

  function fmtMhz(value) {
    var n = num(value);
    if (!n) {
      return '—';
    }
    return Math.round(n).toLocaleString('zh-CN') + ' MHz';
  }

  // ------------------------------------------------------------------
  // 状态
  // ------------------------------------------------------------------
  var state = {
    board: { ok: false, fails: 0, lastOk: 0, lastErr: null, err: '', detail: '',
             stats: null, passthrough: false },
    pc: { ok: false, fails: 0, lastOk: 0, lastErr: null, data: null },
    procSort: 'cpu'
  };

  var boardBusy = false;
  var pcBusy = false;

  // ------------------------------------------------------------------
  // fetch + 超时
  // ------------------------------------------------------------------
  function fetchJson(url, timeoutMs) {
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = null;
    var options = { cache: 'no-store', headers: { Accept: 'application/json' } };
    if (controller) {
      options.signal = controller.signal;
      timer = setTimeout(function () {
        try {
          controller.abort();
        } catch (e) { /* ignore */ }
      }, timeoutMs);
    }
    return fetch(url, options).then(function (res) {
      if (!res.ok) {
        throw new Error('HTTP ' + res.status);
      }
      return res.json();
    }).then(function (data) {
      if (timer) {
        clearTimeout(timer);
      }
      return data;
    }, function (err) {
      if (timer) {
        clearTimeout(timer);
      }
      throw err;
    });
  }

  // 发一条命令给板子（hub 的 POST /api/cmd，白名单命令）
  function sendBoardCmd(payload) {
    return fetch(API_CMD, {
      method: 'POST',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().catch(function () {
        return { ok: false, err: 'bad_response', detail: 'HTTP ' + res.status };
      });
    });
  }

  // ==================================================================
  // 画布组件
  // ==================================================================
  function Gauge(canvas, color) {
    this.canvas = canvas;
    this.ctx = canvas ? canvas.getContext('2d') : null;
    this.color = color;
    this.value = null;
    this.w = 0;
    this.h = 0;
    this.resizeIfNeeded();
  }

  Gauge.prototype.resizeIfNeeded = function () {
    if (!this.canvas || !this.ctx) {
      return;
    }
    var w = this.canvas.clientWidth || 96;
    var h = this.canvas.clientHeight || 96;
    if (w === this.w && h === this.h) {
      return;
    }
    var dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w;
    this.h = h;
    this.draw();
  };

  Gauge.prototype.set = function (value) {
    var n = num(value);
    this.value = n === null ? null : Math.max(0, Math.min(100, n));
    this.draw();
  };

  Gauge.prototype.draw = function () {
    if (!this.canvas || !this.ctx) {
      return;
    }
    this.resizeIfNeeded();
    var ctx = this.ctx;
    var w = this.w;
    var h = this.h;
    var cx = w / 2;
    var cy = h / 2;
    var radius = Math.max(10, Math.min(w, h) / 2 - 8);
    var start = Math.PI * 0.75;
    var sweep = Math.PI * 1.5;

    ctx.clearRect(0, 0, w, h);

    ctx.lineCap = 'round';
    ctx.lineWidth = Math.max(6, radius * 0.22);
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.16)';
    ctx.beginPath();
    ctx.arc(cx, cy, radius, start, start + sweep);
    ctx.stroke();

    if (this.value !== null) {
      var frac = this.value / 100;
      ctx.strokeStyle = this.color;
      ctx.shadowColor = this.color;
      ctx.shadowBlur = 10;
      ctx.beginPath();
      ctx.arc(cx, cy, radius, start, start + sweep * frac);
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    ctx.fillStyle = '#e6edf3';
    ctx.font = '700 ' + Math.round(Math.max(14, radius * 0.46)) + 'px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    var label = this.value === null ? '—' : this.value.toFixed(0) + '%';
    ctx.fillText(label, cx, cy + 1);
  };

  function LineChart(canvas, colors) {
    this.canvas = canvas;
    this.ctx = canvas ? canvas.getContext('2d') : null;
    this.colors = colors;
    this.history = [];
    for (var i = 0; i < colors.length; i += 1) {
      this.history.push([]);
    }
    this.w = 0;
    this.h = 0;
    this.resizeIfNeeded();
  }

  LineChart.prototype.resizeIfNeeded = function () {
    if (!this.canvas || !this.ctx) {
      return;
    }
    var w = this.canvas.clientWidth || 320;
    var h = this.canvas.clientHeight || 170;
    if (w === this.w && h === this.h) {
      return;
    }
    var dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w;
    this.h = h;
    this.draw();
  };

  LineChart.prototype.push = function (values) {
    for (var i = 0; i < this.history.length; i += 1) {
      var series = this.history[i];
      var v = num(values[i]);
      series.push(v === null ? null : Math.max(0, Math.min(100, v)));
      while (series.length > HISTORY_LEN) {
        series.shift();
      }
    }
    this.draw();
  };

  LineChart.prototype.draw = function () {
    if (!this.canvas || !this.ctx) {
      return;
    }
    this.resizeIfNeeded();
    var ctx = this.ctx;
    var w = this.w;
    var h = this.h;
    var padL = 32;
    var padR = 8;
    var padT = 8;
    var padB = 16;
    var plotW = w - padL - padR;
    var plotH = h - padT - padB;

    ctx.clearRect(0, 0, w, h);

    ctx.font = '10px system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (var p = 0; p <= 100; p += 25) {
      var y = padT + plotH * (1 - p / 100);
      ctx.strokeStyle = p === 0 ? 'rgba(148,163,184,0.22)' : 'rgba(148,163,184,0.10)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, y + 0.5);
      ctx.lineTo(padL + plotW, y + 0.5);
      ctx.stroke();
      ctx.fillStyle = 'rgba(139,152,169,0.75)';
      ctx.fillText(p + '%', padL - 5, y);
    }

    for (var k = 0; k <= 4; k += 1) {
      var x = padL + plotW * (k / 4);
      ctx.strokeStyle = 'rgba(148,163,184,0.06)';
      ctx.beginPath();
      ctx.moveTo(x + 0.5, padT);
      ctx.lineTo(x + 0.5, padT + plotH);
      ctx.stroke();
    }

    for (var s = 0; s < this.history.length; s += 1) {
      var data = this.history[s];
      if (!data.length) {
        continue;
      }
      var offset = HISTORY_LEN - data.length;
      var started = false;
      ctx.beginPath();
      for (var i = 0; i < data.length; i += 1) {
        var value = data[i];
        if (value === null) {
          started = false;
          continue;
        }
        var px = padL + plotW * ((offset + i) / (HISTORY_LEN - 1));
        var py = padT + plotH * (1 - value / 100);
        if (!started) {
          ctx.moveTo(px, py);
          started = true;
        } else {
          ctx.lineTo(px, py);
        }
      }
      ctx.strokeStyle = this.colors[s];
      ctx.lineWidth = 1.8;
      ctx.lineJoin = 'round';
      ctx.stroke();
    }
  };

  // ==================================================================
  // 板子面板渲染
  // ==================================================================
  var gauge0 = null;
  var gauge1 = null;
  var cpuChart = null;
  var gaugePc = null;

  function renderBoard(stats) {
    var cpu = stats.cpu || {};
    var mem = stats.mem || {};
    var sd = stats.sd || {};
    var net = stats.net || {};
    var hid = stats.hid || {};
    var uart = stats.uart || {};

    setText('board-meta', stats.proto ? 'proto ' + stats.proto : 'proto —');
    setText('board-uptime', fmtDurationMs(stats.uptime_ms));
    setText('board-freq', fmtMhz(cpu.mhz));
    setText('board-window', cpu.window_ms ? Math.round(num(cpu.window_ms)) + ' ms' : '—');
    setText('cpu-window-hint', cpu.window_ms ? '统计窗口 ' + Math.round(num(cpu.window_ms)) + ' ms' : '');

    // 板子内部温度（v1.2 可选字段：temp_c / temp_warn；缺失时优雅降级为 —）
    var tempEl = $('board-temp');
    var tempC = num(stats.temp_c);
    var tempWarn = stats.temp_warn === true;
    if (tempC === null) {
      setText('board-temp', tempWarn ? '过热 ⚠' : '—');
    } else {
      setText('board-temp', tempC.toFixed(1) + ' °C' + (tempWarn ? ' ⚠ 过热' : ''));
    }
    toggleClass(tempEl, 'bad', tempWarn);
    toggleClass(tempEl, 'warn', !tempWarn && tempC !== null && tempC >= 70);
    toggleClass(tempEl, 'good', !tempWarn && tempC !== null && tempC < 70);

    // HID
    var hidEl = $('board-hid');
    var mounted = hid.mounted === true;
    setText('board-hid', mounted ? '已枚举' : '未枚举');
    toggleClass(hidEl, 'good', mounted);
    toggleClass(hidEl, 'bad', !mounted);

    // 双核 CPU
    var core0 = num(cpu.core0_pct);
    var core1 = num(cpu.core1_pct);
    setText('cpu-val0', core0 === null ? '—' : core0.toFixed(1) + '%');
    setText('cpu-val1', core1 === null ? '—' : core1.toFixed(1) + '%');
    if (gauge0) {
      gauge0.set(core0);
    }
    if (gauge1) {
      gauge1.set(core1);
    }
    if (cpuChart) {
      cpuChart.push([core0, core1]);
    }

    // 内存
    var heapFree = num(mem.heap_free);
    var heapTotal = num(mem.heap_total);
    var heapPct = heapTotal ? (heapFree / heapTotal) * 100 : null;
    setText('heap-free', fmtBytes(heapFree));
    setText('heap-min', fmtBytes(mem.heap_min_free));
    setText('heap-total', fmtBytes(heapTotal));
    setBar('heap-bar', heapPct);
    setText('heap-pct', heapPct === null ? '—' : heapPct.toFixed(1) + '%');

    var psramFree = num(mem.psram_free);
    var psramTotal = num(mem.psram_total);
    var psramPct = psramTotal ? (psramFree / psramTotal) * 100 : null;
    setText('psram-free', fmtBytes(psramFree));
    setText('psram-total', fmtBytes(psramTotal));
    setBar('psram-bar', psramPct);
    setText('psram-pct', psramPct === null ? '—' : psramPct.toFixed(1) + '%');

    // TF 卡
    var sdState = $('sd-state');
    if (sd.present === false) {
      setText('sd-state', '未插卡');
      sdState && sdState.classList.remove('good', 'bad', 'warn');
      setText('sd-type', '—');
      setText('sd-capacity', '—');
      setText('sd-free', '—');
      setText('sd-cid', '—');
    } else if (sd.mounted !== true) {
      setText('sd-state', '挂载失败' + (sd.err ? '：' + sd.err : ''));
      toggleClass(sdState, 'bad', true);
      toggleClass(sdState, 'good', false);
      setText('sd-type', sd.type || '—');
      setText('sd-capacity', '—');
      setText('sd-free', '—');
      setText('sd-cid', sd.cid || '—');
    } else {
      setText('sd-state', '已挂载');
      toggleClass(sdState, 'good', true);
      toggleClass(sdState, 'bad', false);
      setText('sd-type', (sd.type || '—') + (sd.fs ? ' · ' + sd.fs : ''));
      setText('sd-capacity', fmtMb(sd.capacity_mb));
      setText('sd-free', sd.free_mb === null || sd.free_mb === undefined ? '—' : fmtMb(sd.free_mb));
      setText('sd-cid', sd.cid || '—');
    }
    setText('sd-write', fmtKbps(sd.speed_write_kbps));
    setText('sd-read', fmtKbps(sd.speed_read_kbps));

    // 动态调频开关（v1.2 可选字段 stats.pm；固件没上报时开关禁用并说明）
    var pm = stats.pm || {};
    var pmKnown = pm.dfs === true || pm.dfs === false;
    var pmOn = pm.dfs === true;
    var pmToggle = $('pm-toggle');
    if (pmToggle && !pmSending) {
      pmToggle.checked = pmOn;
      pmToggle.disabled = !pmKnown;
    }
    setText('pm-state', !pmKnown ? '固件未上报' : (pmOn ? '已开启' : '已关闭'));
    var pmStateEl = $('pm-state');
    toggleClass(pmStateEl, 'good', pmKnown && !pmOn);
    toggleClass(pmStateEl, 'warn', pmKnown && pmOn);
    setText('pm-title', !pmKnown ? '当前固件不支持切换'
      : (pmOn ? '省电模式 · 动态调频' : '稳定模式 · 固定 360MHz'));
    if (!pmKnown) {
      setText('pm-hint', '固件升级到带 pm 字段的版本后，这个开关才能用。');
    } else if (pmOn) {
      setText('pm-hint', '空闲时自动降频省电；若出现鼠标断续或卡异常，请关掉。');
    } else {
      setText('pm-hint', '固定 360MHz，SD 卡与 USB 最稳；发热实测约 24°C，无需担心。');
    }
    setText('pm-cur', pm.cur_mhz ? fmtMhz(pm.cur_mhz) : fmtMhz(cpu.mhz));
    setText('pm-range', pm.min_mhz && pm.max_mhz
      ? fmtMhz(pm.min_mhz) + ' – ' + fmtMhz(pm.max_mhz)
      : '—');
    setText('pm-gov', !pmKnown ? '—'
      : (pm.governor === 'adaptive' ? '智能调度' : '固定 360'));
    if (!pmKnown || pm.boosts === undefined) {
      setText('pm-counts', '—');
    } else {
      var counts = '升 ' + (pm.boosts || 0) + ' / 降 ' + (pm.downs || 0);
      if (pm.last_reason) {
        counts += ' · 上次：' + pm.last_reason;
      }
      setText('pm-counts', counts);
    }

    // 物理频率自检：固件用 mcycle/µs 独立测得（不是读 PM 状态），可用来验证开关不是摆设
    var measured = num(pm.measured_mhz);
    setText('pm-measured', measured === null ? '—' : Math.round(measured) + ' MHz');
    var checkEl = $('pm-selfcheck');
    if (measured === null || !pmKnown) {
      setText('pm-selfcheck', '—');
      toggleClass(checkEl, 'good', false);
      toggleClass(checkEl, 'bad', false);
    } else if (pmOn) {
      var downed = measured < 300;
      setText('pm-selfcheck', downed ? '已降频 ✓' : '未见降频 ⚠');
      toggleClass(checkEl, 'good', downed);
      toggleClass(checkEl, 'bad', !downed);
    } else {
      var full = measured >= 300;
      setText('pm-selfcheck', full ? '满频 ✓' : '未满频 ⚠');
      toggleClass(checkEl, 'good', full);
      toggleClass(checkEl, 'bad', !full);
    }

    // 网络
    var netState = $('net-state');
    var up = net.up === true;
    setText('net-state', up ? '已连接' : '未连接');
    toggleClass(netState, 'good', up);
    toggleClass(netState, 'bad', !up);
    setText('net-ip', net.ip || '—');
    setText('net-link', net.link_mbps ? Math.round(num(net.link_mbps)) + ' Mbps' : '—');
    setText('net-dhcp', net.dhcp === true ? '是' : net.dhcp === false ? '否' : '—');
    setText('net-gw', net.gw || '—');
    setText('net-mac', net.mac || '—');

    // UART
    setText('uart-rx-ok', fmtInt(uart.rx_ok));
    setText('uart-rx-bad', fmtInt(uart.rx_bad));
    setText('uart-tx-ok', fmtInt(uart.tx_ok));
    var badEl = $('uart-rx-bad');
    toggleClass(badEl, 'bad', num(uart.rx_bad) > 0);

    // 任务表
    renderTasks(stats.tasks);
  }

  function setBar(id, pct) {
    var el = $(id);
    if (!el) {
      return;
    }
    el.style.width = pct === null ? '0%' : Math.max(0, Math.min(100, pct)).toFixed(1) + '%';
  }

  function renderTasks(tasks) {
    var body = $('task-tbody');
    if (!body) {
      return;
    }
    body.textContent = '';
    if (!Array.isArray(tasks) || tasks.length === 0) {
      setText('task-count', '0 个任务');
      var tr = document.createElement('tr');
      var td = document.createElement('td');
      td.colSpan = 4;
      td.className = 'muted';
      td.textContent = '暂无任务数据';
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    setText('task-count', tasks.length + ' 个任务');
    for (var i = 0; i < tasks.length; i += 1) {
      var task = tasks[i] || {};
      var row = document.createElement('tr');
      row.appendChild(cell(task.name || '?', ''));
      row.appendChild(cell(task.prio === undefined ? '—' : fmtInt(task.prio), 'num'));
      row.appendChild(cell(task.state || '—', 'muted'));
      row.appendChild(cell(task.stack_hwm === undefined || task.stack_hwm === null ? '—' : fmtInt(task.stack_hwm), 'num'));
      body.appendChild(row);
    }
  }

  function cell(text, cls) {
    var td = document.createElement('td');
    if (cls) {
      td.className = cls;
    }
    td.textContent = text;
    return td;
  }

  // ==================================================================
  // 本机面板渲染
  // ==================================================================
  var coreEls = [];

  function renderPc(data) {
    var cpu = data.cpu || {};
    var mem = data.mem || {};
    var cores = Array.isArray(cpu.cores) ? cpu.cores : [];

    setText('pc-cpu-total-val', fmtPct(cpu.total_pct));
    setText('pc-cpu-total-ring', fmtPct(cpu.total_pct, 0));
    setText('pc-cpu-mhz', fmtMhz(cpu.mhz));
    setText('pc-core-count', cores.length ? cores.length + ' 个逻辑核' : '—');
    if (gaugePc) {
      gaugePc.set(num(cpu.total_pct));
    }
    updateCoreBars(cores);

    setText('pc-mem-used', fmtMb(mem.used_mb));
    setText('pc-mem-total', fmtMb(mem.total_mb));
    setText('pc-mem-pct', fmtPct(mem.pct));
    setBar('pc-mem-bar', num(mem.pct));
    setText('pc-mem-extra', mem.total_mb ? '总 ' + fmtMb(mem.total_mb) : '—');

    renderGpu(data.gpu);
    renderNpu(data.npu);
    renderProcesses(data.processes);
    setText('pc-uptime', fmtDuration(data.uptime_s));
  }

  function updateCoreBars(cores) {
    var box = $('pc-cores');
    if (!box) {
      return;
    }
    if (coreEls.length !== cores.length || box.childElementCount !== cores.length) {
      box.textContent = '';
      coreEls = [];
      for (var i = 0; i < cores.length; i += 1) {
        var row = document.createElement('div');
        row.className = 'core';

        var label = document.createElement('span');
        label.className = 'core-label';
        label.textContent = 'C' + i;

        var track = document.createElement('div');
        track.className = 'core-track';
        var fill = document.createElement('i');
        fill.className = 'core-fill';
        track.appendChild(fill);

        var val = document.createElement('span');
        val.className = 'core-val';
        val.textContent = '—';

        row.appendChild(label);
        row.appendChild(track);
        row.appendChild(val);
        box.appendChild(row);
        coreEls.push({ fill: fill, val: val });
      }
    }
    for (var j = 0; j < cores.length; j += 1) {
      var pct = num(cores[j]);
      var el = coreEls[j];
      if (!el) {
        continue;
      }
      el.fill.style.width = (pct === null ? 0 : Math.max(0, Math.min(100, pct))) + '%';
      el.fill.className = 'core-fill' + (pct !== null && pct >= 85 ? ' critical' : pct !== null && pct >= 65 ? ' hot' : '');
      el.val.textContent = pct === null ? '—' : pct.toFixed(0) + '%';
    }
  }

  function renderGpu(gpus) {
    var box = $('pc-gpu-list');
    if (!box) {
      return;
    }
    box.textContent = '';
    if (!Array.isArray(gpus) || gpus.length === 0) {
      var none = document.createElement('div');
      none.className = 'npu not-detected';
      none.textContent = '未检测到 NVIDIA GPU（nvidia-smi 不可用）';
      box.appendChild(none);
      return;
    }
    for (var i = 0; i < gpus.length; i += 1) {
      var gpu = gpus[i] || {};
      var card = document.createElement('div');
      card.className = 'gpu-card';

      var name = document.createElement('div');
      name.className = 'gpu-name';
      name.textContent = gpu.name || 'NVIDIA GPU';
      card.appendChild(name);

      var util = num(gpu.util_pct);
      var row = document.createElement('div');
      row.className = 'gpu-row';
      var track = document.createElement('div');
      track.className = 'bar-track';
      var fill = document.createElement('i');
      fill.className = 'bar-fill';
      fill.style.width = (util === null ? 0 : Math.max(0, Math.min(100, util))) + '%';
      track.appendChild(fill);
      row.appendChild(track);
      var utilText = document.createElement('b');
      utilText.textContent = util === null ? '—' : util.toFixed(0) + '%';
      row.appendChild(utilText);
      card.appendChild(row);

      var stats = document.createElement('div');
      stats.className = 'gpu-stats';
      stats.appendChild(stat('温度', num(gpu.temp_c) === null ? '—' : num(gpu.temp_c).toFixed(0) + ' °C'));
      stats.appendChild(stat('显存', gpu.mem_used_mb === null || gpu.mem_used_mb === undefined
        ? '—'
        : fmtMb(gpu.mem_used_mb) + ' / ' + fmtMb(gpu.mem_total_mb)));
      card.appendChild(stats);
      box.appendChild(card);
    }
  }

  function stat(label, value) {
    var span = document.createElement('span');
    span.appendChild(document.createTextNode(label + ' '));
    var b = document.createElement('b');
    b.textContent = value;
    span.appendChild(b);
    return span;
  }

  function renderNpu(npu) {
    var el = $('pc-npu');
    if (!el) {
      return;
    }
    if (npu && npu.detected === true && npu.name) {
      el.textContent = '已检测：' + npu.name;
      el.className = 'npu detected';
    } else {
      el.textContent = '未检测（本机没有可用 NPU，或驱动未暴露）';
      el.className = 'npu not-detected';
    }
  }

  function renderProcesses(processes) {
    var body = $('pc-proc-body');
    if (!body) {
      return;
    }
    body.textContent = '';
    if (!Array.isArray(processes) || processes.length === 0) {
      setText('pc-proc-count', '0 个进程');
      var emptyRow = document.createElement('tr');
      var emptyCell = document.createElement('td');
      emptyCell.colSpan = 4;
      emptyCell.className = 'muted';
      emptyCell.textContent = '暂无进程数据（PowerShell 采样中…）';
      emptyRow.appendChild(emptyCell);
      body.appendChild(emptyRow);
      return;
    }
    var rows = processes.slice();
    var sortBy = state.procSort;
    rows.sort(function (a, b) {
      if (sortBy === 'mem') {
        return (num(b.mem_mb) || 0) - (num(a.mem_mb) || 0);
      }
      var ca = num(a.cpu_pct);
      var cb = num(b.cpu_pct);
      if (ca === null && cb === null) {
        return (num(b.mem_mb) || 0) - (num(a.mem_mb) || 0);
      }
      if (ca === null) {
        return 1;
      }
      if (cb === null) {
        return -1;
      }
      return cb - ca;
    });

    setText('pc-proc-count', rows.length + ' 个进程');

    for (var i = 0; i < rows.length; i += 1) {
      var p = rows[i] || {};
      var tr = document.createElement('tr');
      var nameCell = cell(p.name || '?', '');
      nameCell.title = p.name || '';
      tr.appendChild(nameCell);
      tr.appendChild(cell(p.pid === undefined ? '—' : fmtInt(p.pid), 'num mono'));
      var cpuPct = num(p.cpu_pct);
      tr.appendChild(cell(cpuPct === null ? '—' : cpuPct.toFixed(1), 'num'));
      tr.appendChild(cell(fmtMb(p.mem_mb), 'num'));
      body.appendChild(tr);
    }
  }

  // ==================================================================
  // 轮询：板子
  // ==================================================================
  function pollBoard() {
    if (boardBusy) {
      return;
    }
    boardBusy = true;
    fetchJson(API_BOARD, BOARD_TIMEOUT_MS).then(function (raw) {
      if (!raw || typeof raw !== 'object') {
        throw new Error('响应不是 JSON 对象');
      }
      if (raw.ok === true && raw.stats && typeof raw.stats === 'object') {
        state.board.ok = true;
        state.board.fails = 0;
        state.board.lastOk = Date.now();
        state.board.lastErr = null;
        state.board.err = '';
        state.board.detail = '';
        state.board.stats = raw.stats;
        state.board.passthrough = raw.passthrough === true;
        renderBoard(raw.stats);
        toggleHidden($('board-empty'), true);
        toggleHidden($('board-body'), false);
      } else {
        var e = new Error(raw.err || 'board_offline');
        e.code = raw.err || 'board_offline';
        e.detail = raw.detail || '';
        e.passthrough = raw.passthrough === true;
        throw e;
      }
    }).catch(function (err) {
      state.board.ok = false;
      state.board.fails += 1;
      state.board.err = (err && err.code) || '';
      state.board.detail = (err && err.detail) || '';
      state.board.passthrough = err && err.passthrough === true;
      state.board.lastErr = String((err && err.detail) || (err && err.message) || err);
    }).then(function () {
      boardBusy = false;
    });
  }

  // ==================================================================
  // 轮询：本机
  // ==================================================================
  function pollPc() {
    if (pcBusy) {
      return;
    }
    pcBusy = true;
    fetchJson(API_PC, PC_TIMEOUT_MS).then(function (data) {
      if (!data || data.ok === false) {
        throw new Error((data && (data.error || data.detail)) || '响应格式不符');
      }
      state.pc.ok = true;
      state.pc.fails = 0;
      state.pc.lastOk = Date.now();
      state.pc.lastErr = null;
      state.pc.data = data;
      renderPc(data);
      toggleHidden($('pc-empty'), true);
      toggleHidden($('pc-body'), false);
    }).catch(function (err) {
      state.pc.ok = false;
      state.pc.fails += 1;
      state.pc.lastErr = String((err && err.message) || err);
    }).then(function () {
      pcBusy = false;
    });
  }

  // ==================================================================
  // 状态胶囊 / 断线提示 / 超时标灰
  // ==================================================================
  function boardOfflineHint() {
    switch (state.board.err) {
      case 'board_offline':
        return 'hub 打不开串口 COM7（板子没插 / 串口被占用 / COM 号变了）。hub 会自动重试。';
      case 'board_busy':
        return '板子锁被其他程序占用（可能是 fw-core 烧录或 draw 在画图），hub 会等锁释放后自动接管。';
      case 'board_no_stats':
        return '串口已连上，但固件没有返回 stats 数据（固件可能还没升级到 v1.2）。hub 会继续重试。';
      default:
        return 'hub 暂时不可用：' + (state.board.lastErr || '未知错误');
    }
  }

  function updateStatuses() {
    var now = Date.now();

    // ---- 板子 ----
    var boardPill = $('board-status');
    var boardPanel = $('board-panel');
    var boardAge = state.board.lastOk ? now - state.board.lastOk : Infinity;
    var boardStale = boardAge > STALE_MS;
    var boardBanner = $('board-banner');
    var hasStats = Boolean(state.board.stats);
    var errText = BOARD_ERR_TEXT[state.board.err];

    toggleClass(boardPanel, 'stale', hasStats && (boardStale || !state.board.ok || Boolean(errText)));

    if (state.board.passthrough) {
      // hub 把串口暂时让给 brain.py 的 TCP 透传：stats 暂停，不是故障
      boardPill.textContent = '板子透传中';
      boardPill.className = 'pill pill-warn';
      setText('board-updated', '透传中 · 最后更新 ' + fmtAgo(boardAge));
      toggleClass(boardPanel, 'stale', true);
      if (boardBanner) {
        boardBanner.classList.remove('hidden');
        boardBanner.classList.add('warn');
        boardBanner.classList.remove('err');
        boardBanner.textContent = '板子串口正被 TCP 透传占用（brain.py），stats 暂停刷新；'
          + '透传结束后会自动恢复（数据超过 3.2 s 未更新会标灰）。';
      }
      if (hasStats) {
        toggleHidden($('board-empty'), true);
        toggleHidden($('board-body'), false);
      }
    } else if (state.board.ok && !boardStale) {
      boardPill.textContent = '板子在线';
      boardPill.className = 'pill pill-ok';
      toggleHidden(boardBanner, true);
      setText('board-updated', '更新于 ' + fmtAgo(boardAge));
      toggleHidden($('board-empty'), true);
      toggleHidden($('board-body'), false);
    } else if (errText) {
      // hub 明确报错：离线 / 被占用 / 固件无 stats
      boardPill.textContent = errText;
      boardPill.className = 'pill pill-err';
      setText('board-updated', hasStats ? '最后更新 ' + fmtAgo(boardAge) : '离线');
      if (boardBanner) {
        boardBanner.classList.remove('hidden');
        boardBanner.classList.add('err');
        boardBanner.classList.remove('warn');
        boardBanner.textContent = boardOfflineHint();
      }
      if (hasStats) {
        // 保留最后一次数据，但已标灰
        toggleHidden($('board-empty'), true);
        toggleHidden($('board-body'), false);
      } else {
        setText('board-empty-text', '板子暂无数据');
        setText('board-empty-hint', boardOfflineHint());
        toggleHidden($('board-empty'), false);
        toggleHidden($('board-body'), true);
      }
    } else if (hasStats) {
      boardPill.textContent = '板子数据超时';
      boardPill.className = 'pill pill-warn';
      setText('board-updated', '最后更新 ' + fmtAgo(boardAge));
      if (boardBanner) {
        boardBanner.classList.remove('hidden');
        boardBanner.classList.toggle('err', !state.board.ok);
        boardBanner.classList.toggle('warn', state.board.ok);
        boardBanner.textContent = '板子数据已 ' + Math.round(boardAge / 1000) + ' s 未更新，正在自动重连…（'
          + (state.board.lastErr || '未收到错误信息') + '）';
      }
      toggleHidden($('board-empty'), true);
      toggleHidden($('board-body'), false);
    } else if (state.board.fails > 0) {
      boardPill.textContent = '板子离线';
      boardPill.className = 'pill pill-err';
      setText('board-updated', '离线');
      if (boardBanner) {
        boardBanner.classList.remove('hidden');
        boardBanner.classList.add('err');
        boardBanner.classList.remove('warn');
        boardBanner.textContent = boardOfflineHint();
      }
      setText('board-empty-text', '板子暂无数据');
      setText('board-empty-hint', boardOfflineHint());
      toggleHidden($('board-empty'), false);
      toggleHidden($('board-body'), true);
    } else {
      boardPill.textContent = '板子连接中…';
      boardPill.className = 'pill pill-wait';
    }

    // ---- 本机 ----
    var pcPill = $('pc-status');
    var pcPanel = $('pc-panel');
    var pcAge = state.pc.lastOk ? now - state.pc.lastOk : Infinity;
    var pcStale = pcAge > STALE_MS;
    var pcBanner = $('pc-banner');

    toggleClass(pcPanel, 'stale', Boolean(state.pc.data) && (pcStale || !state.pc.ok));

    if (state.pc.ok && !pcStale) {
      pcPill.textContent = '本机在线';
      pcPill.className = 'pill pill-ok';
      toggleHidden(pcBanner, true);
      setText('pc-updated', '更新于 ' + fmtAgo(pcAge));
    } else if (state.pc.data) {
      pcPill.textContent = '本机数据超时';
      pcPill.className = 'pill pill-warn';
      setText('pc-updated', '最后更新 ' + fmtAgo(pcAge));
      if (pcBanner) {
        pcBanner.classList.remove('hidden');
        pcBanner.classList.add('warn');
        pcBanner.classList.remove('err');
        pcBanner.textContent = '本机数据已 ' + Math.round(pcAge / 1000) + ' s 未更新，正在自动重连…';
      }
    } else if (state.pc.fails > 0) {
      pcPill.textContent = '本机未连接';
      pcPill.className = 'pill pill-err';
      setText('pc-updated', '等待数据');
      if (pcBanner) {
        pcBanner.classList.remove('hidden');
        pcBanner.classList.add('err');
        pcBanner.classList.remove('warn');
        pcBanner.textContent = '无法读取 api/pc：' + (state.pc.lastErr || '未知错误')
          + '。hub 可能刚启动还在采样（1–2 s），已重试 ' + state.pc.fails + ' 次。';
      }
    } else {
      pcPill.textContent = '本机连接中…';
      pcPill.className = 'pill pill-wait';
    }

    var t = new Date();
    setText('footer-time', pad2(t.getHours()) + ':' + pad2(t.getMinutes()) + ':' + pad2(t.getSeconds()));
  }

  // ==================================================================
  // 初始化
  // ==================================================================
  function bindEvents() {
    var refresh = $('refresh-btn');
    if (refresh) {
      refresh.addEventListener('click', function () {
        pollBoard();
        pollPc();
      });
    }

    // 动态调频开关：POST /api/cmd -> hub -> 板子 set pm/dfs
    var pmToggle = $('pm-toggle');
    if (pmToggle) {
      pmToggle.addEventListener('change', function () {
        var want = pmToggle.checked;
        pmSending = true;
        pmToggle.disabled = true;
        setText('pm-state', '切换中…');
        sendBoardCmd({ cmd: 'set', key: 'dfs', value: want ? 1 : 0 }).then(function (resp) {
          if (resp && resp.ok) {
            setText('pm-hint', '已切换到' + (want ? '省电模式（动态调频）' : '稳定模式（固定 360MHz）') + '。');
            pollBoard();
          } else {
            pmToggle.checked = !want;
            setText('pm-hint', '切换失败：' + ((resp && (resp.err || resp.detail)) || '未知错误'));
          }
        }, function (err) {
          pmToggle.checked = !want;
          setText('pm-hint', '切换失败：' + (err && err.message ? err.message : err));
        }).then(function () {
          pmSending = false;
          pmToggle.disabled = false;
        });
      });
    }

    var sortCpu = $('sort-cpu');
    var sortMem = $('sort-mem');    if (sortCpu) {
      sortCpu.addEventListener('click', function () {
        state.procSort = 'cpu';
        toggleClass(sortCpu, 'chip-active', true);
        toggleClass(sortMem, 'chip-active', false);
        if (state.pc.data) {
          renderProcesses(state.pc.data.processes);
        }
      });
    }
    if (sortMem) {
      sortMem.addEventListener('click', function () {
        state.procSort = 'mem';
        toggleClass(sortMem, 'chip-active', true);
        toggleClass(sortCpu, 'chip-active', false);
        if (state.pc.data) {
          renderProcesses(state.pc.data.processes);
        }
      });
    }

    var resizeTimer = null;
    window.addEventListener('resize', function () {
      if (resizeTimer) {
        clearTimeout(resizeTimer);
      }
      resizeTimer = setTimeout(function () {
        if (gauge0) {
          gauge0.w = 0;
          gauge0.resizeIfNeeded();
        }
        if (gauge1) {
          gauge1.w = 0;
          gauge1.resizeIfNeeded();
        }
        if (gaugePc) {
          gaugePc.w = 0;
          gaugePc.resizeIfNeeded();
        }
        if (cpuChart) {
          cpuChart.w = 0;
          cpuChart.resizeIfNeeded();
        }
      }, 120);
    });
  }

  function init() {
    gauge0 = new Gauge($('cpu-gauge0'), '#22d3ee');
    gauge1 = new Gauge($('cpu-gauge1'), '#a78bfa');
    gaugePc = new Gauge($('pc-cpu-gauge'), '#a78bfa');
    cpuChart = new LineChart($('cpu-chart'), ['#22d3ee', '#a78bfa']);
    cpuChart.resizeIfNeeded();

    bindEvents();

    // 轮询：每秒；用 inFlight 防重入
    setInterval(pollBoard, BOARD_POLL_MS);
    setInterval(pollPc, PC_POLL_MS);
    pollBoard();
    pollPc();

    // 状态/超时检查：每 250 ms（比轮询更细，断线时标灰更及时）
    setInterval(updateStatuses, 250);
    updateStatuses();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
