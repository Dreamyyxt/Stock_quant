/**
 * QuantOS 页面渲染探针（T-302 验收的前端一半）
 * ===========================================
 *
 * 用法：
 *   node scripts/page_probe.js <index.html> <fixture.json|->  
 *
 * fixture 为 `-` 时模拟「运维服务不可达」（演示模式）；
 * 否则按 /api/ops/* 路径返回 fixture 里的 snapshot / strategies / audit。
 *
 * 为什么不用 jsdom/puppeteer：本机没有这些依赖，而这个页面只需要一个
 * 极小的 DOM 子集就能验证「渲染出来的数字是否等于后端 JSON」。
 * 判据不是"页面能打开"，而是逐项比对数字。
 */
'use strict';

const fs = require('fs');
const vm = require('vm');
const path = require('path');

const [, , htmlPath, fixturePath] = process.argv;
if (!htmlPath) {
  console.error('用法: node page_probe.js <index.html> <fixture.json|->');
  process.exit(2);
}

const fixture = (!fixturePath || fixturePath === '-')
  ? null
  : JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

/* fixture 必须是三个接口的合并，缺一个就会在后面报出难懂的 undefined 错误。
   直接在这里拦掉并说清结构，比让调用方去读堆栈强。 */
if (fixture) {
  const missing = ['snapshot', 'strategies', 'audit']
    .filter(k => !fixture[k] || typeof fixture[k] !== 'object');
  if (missing.length) {
    console.error(
      'fixture 结构不对，缺少：' + missing.join(', ') + '\n' +
      '应为 {"snapshot": <GET /api/ops/snapshot 的返回>,\n' +
      '      "strategies": <GET /api/ops/strategies>,\n' +
      '      "audit": <GET /api/ops/audit>}\n' +
      '生成方式见 scripts/quantos_selftest_b3.py 的 test_page()。');
    process.exit(2);
  }
}

function mkClassList() {
  const s = new Set();
  return {
    add() { for (const a of arguments) s.add(a); },
    remove() { for (const a of arguments) s.delete(a); },
    toggle(c, on) {
      if (on === undefined) { s.has(c) ? s.delete(c) : s.add(c); }
      else { on ? s.add(c) : s.delete(c); }
    },
    contains(c) { return s.has(c); }
  };
}

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attrs = {};
    this.style = {};
    this._html = '';
    this._t = '';
    this.classList = mkClassList();
    this.files = null;
    this.value = '';
    this.parentNode = null;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] !== undefined ? this.attrs[k] : null; }
  hasAttribute(k) { return this.attrs[k] !== undefined; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); }
  addEventListener() {}
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  click() {}
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  set textContent(v) { this._t = String(v); }
  get textContent() { return this._t; }
}

const registry = {};
const doc = {
  readyState: 'complete',
  documentElement: new El('html'),
  body: new El('body'),
  addEventListener() {},
  getElementById(id) {
    if (!registry[id]) registry[id] = new El('div');
    return registry[id];
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(t) { return new El(t); }
};

const store = {};

function MockXHR() {
  this.headers = {};
  this.status = 0;
  this.responseText = '';
  this.timeout = 0;
}
MockXHR.prototype.open = function (method, url) {
  this.method = method;
  this.url = url;
};
MockXHR.prototype.setRequestHeader = function (k, v) { this.headers[k] = v; };
MockXHR.prototype.send = function (body) {
  const self = this;
  const url = this.url || '';
  setTimeout(function () {
    if (!fixture) {
      self.status = 0;
      if (self.onerror) self.onerror();
      return;
    }
    let data = null;
    if (url.indexOf('/api/ops/snapshot') === 0) data = fixture.snapshot;
    else if (url.indexOf('/api/ops/strategies') === 0) data = fixture.strategies;
    else if (url.indexOf('/api/ops/audit') === 0) data = fixture.audit;
    else if (url.indexOf('/api/ops/nav-history') === 0) data = fixture.navHistory;
    else if (url.indexOf('/api/ops/live') === 0) data = fixture.live;
    else if (url.indexOf('/api/ops/portfolio') === 0) data = fixture.portfolio;
    else if (url.indexOf('/api/ops/schedule') === 0) data = fixture.schedule;
    if (data === null || data === undefined) {
      self.status = 404;
      self.responseText = '{"error":"not found"}';
    } else {
      self.status = 200;
      self.responseText = JSON.stringify(data);
    }
    if (self.onload) self.onload();
  }, 0);
};

const sandbox = {
  window: { scrollTo() {} },
  document: doc,
  localStorage: {
    getItem: k => (store[k] !== undefined ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; }
  },
  XMLHttpRequest: MockXHR,
  console,
  setTimeout, clearTimeout,
  setInterval: () => 0,
  Date, Math, JSON, Number, String, Object, Array, Boolean, isNaN, parseInt, parseFloat,
  Blob: function () {},
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  FileReader: function () {},
  prompt: () => null,
  alert: () => {}
};
sandbox.window.document = doc;
sandbox.window.localStorage = sandbox.localStorage;
sandbox.globalThis = sandbox;

const html = fs.readFileSync(htmlPath, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error('页面里找不到 <script> 块');
  process.exit(2);
}
const code = m[1];

const ctx = vm.createContext(sandbox);
try {
  vm.runInContext(code, ctx, { filename: 'quantos-index.js' });
} catch (e) {
  console.error('RUNTIME_ERROR:', e.message);
  console.error((e.stack || '').split('\n').slice(0, 5).join('\n'));
  process.exit(1);
}

function grab(id) {
  const el = registry[id];
  if (!el) return '(missing)';
  return el._html || el._t || '';
}

/* money() 与页面同算法：四舍五入到整数、千分位、前缀 $。 */
function expectMoney(n) {
  const neg = n < 0;
  let s = String(Math.abs(Math.round(n)));
  s = s.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return (neg ? '-' : '') + '$' + s;
}

/* money2() 同算法，但保留 2 位小数（归因日报 / 实盘限额用）。 */
function expectMoney2(n) {
  const v = Number(n);
  const neg = v < 0;
  const s = Math.abs(v).toFixed(2);
  const dot = s.indexOf('.');
  const head = s.slice(0, dot).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return (neg ? '-' : '') + '$' + head + s.slice(dot);
}

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; return; }
  fail++;
  console.log('FAIL', name, detail === undefined ? '' : ('-> ' + String(detail).slice(0, 160)));
}
function contains(name, haystack, needle) {
  check(name, String(haystack).indexOf(needle) >= 0,
        '缺少 ' + JSON.stringify(needle) + '，实际 ' + String(haystack).slice(0, 120));
}

function run(mode) {
  const liveBar = grab('liveText');
  if (mode === 'offline') {
    contains('离线: 状态条写演示数据', liveBar, '演示数据');
    check('离线: NAV 用演示值', grab('kpiNav') === '$105,200', grab('kpiNav'));
    contains('离线: 策略卡为演示名称', grab('strategyGrid'), '美股 ETF 动量轮动');
    contains('离线: 健康条为演示格', grab('healthStrip'), '美股行情延迟');
    contains('离线: 订单表为演示流水', grab('orderBody'), 'qos-cr-8f1e7');
    check('离线: 数据源标识为模拟', /模拟数据/.test(grab('dataChip')), grab('dataChip'));
    /* B7：这三块在离线时最容易变成"默认看起来没问题"。
       它们必须明说看不到，而不是留空白或写一句"正常"。 */
    ['riskModelBody', 'canaryBody', 'researchBody'].forEach((id, i) => {
      const label = ['风险模型', '灰度路由', 'AI 收件箱'][i];
      contains('离线: ' + label + ' 明说需要运维服务', grab(id), '需要连接运维服务');
    });
    /* B9：排期在离线时最容易被当成"今天没有到期策略"。必须明说看不到。 */
    contains('离线: 策略排期明说需要运维服务', grab('scheduleBody'), '需要连接运维服务');
    return;
  }

  const snap = fixture.snapshot;
  const byKey = {};
  (snap.metrics || []).forEach(mt => { byKey[mt.key] = mt; });

  contains('在线: 状态条写实时数据', liveBar, '实时数据');
  contains('在线: 状态条带摘要', liveBar, snap.headline.slice(0, 8));

  const nav = Number(byKey['portfolio.nav'].value);
  check('在线: NAV = 账本 NAV', grab('kpiNav') === expectMoney(nav),
        grab('kpiNav') + ' vs ' + expectMoney(nav));

  const exp = Number(byKey['portfolio.gross_exposure'].value);
  check('在线: 敞口 = 组合指标', grab('kpiExp') === exp.toFixed(1) + '%',
        grab('kpiExp') + ' vs ' + exp.toFixed(1) + '%');

  const dd = Number(byKey['risk.drawdown'].value);
  check('在线: 回撤 = 风控事实', grab('kpiDd') === dd.toFixed(1) + '%',
        grab('kpiDd') + ' vs ' + dd.toFixed(1) + '%');

  const perm = (snap.facts.risk_state || {}).permission_level;
  contains('在线: 权限显示真实等级', grab('kpiDdSub'), perm);

  const running = (snap.facts.strategies || []).filter(s => s.run_state === 'ACTIVE').length;
  const total = (snap.facts.strategies || []).length;
  check('在线: 策略计数 = 注册表', grab('kpiStrat') === running + ' / ' + total,
        grab('kpiStrat') + ' vs ' + running + ' / ' + total);

  const grid = grab('strategyGrid');
  (snap.facts.strategies || []).forEach(s => {
    contains('在线: 策略卡含 ' + s.strategy_id, grid, s.strategy_id);
  });

  const body = grab('orderBody');
  (snap.facts.all_orders || []).forEach(o => {
    contains('在线: 订单表含 ' + o.client_order_id, body, o.client_order_id);
  });
  const unknown = (snap.facts.all_orders || []).filter(o => o.state === 'UNKNOWN').length;
  contains('在线: 异常笔数与订单一致', grab('orderPending'), unknown + ' 笔异常');

  const audit = grab('auditBody');
  (fixture.audit.records || []).slice(0, 3).forEach(r => {
    contains('在线: 审计表含 ' + r.action + '/' + r.strategy_id, audit, r.strategy_id);
  });
  contains('在线: 审计计数带 digest', grab('auditCount'), fixture.audit.digest.slice(0, 8));

  const cards = grab('healthCards');
  ['行情质量', '行情通道', '执行延迟', '对账', '风控权限'].forEach(label => {
    contains('在线: 健康卡含环节 ' + label, cards, label);
  });

  contains('在线: 数据源标识为实时', grab('dataChip'), '实时数据');

  /* 净值曲线：有真点就画真点，没点就显示占位 —— 两种都不许拿演示曲线顶上。 */
  const navPts = (fixture.navHistory && fixture.navHistory.points) || [];
  const chart = grab('equityChart');
  if (navPts.length >= 2) {
    contains('在线: 净值来源标注为真实', grab('equitySource'), '真实');
    contains('在线: 提示里写明快照点数', grab('equityHint'), String(navPts.length));
    check('在线: 曲线用真实点绘制', chart.indexOf('<svg') >= 0 &&
          chart.indexOf('暂无净值曲线') < 0, '仍是占位');
  } else {
    check('在线: 无净值点时显示占位而非假曲线',
          chart.indexOf('暂无净值曲线') >= 0, chart.slice(0, 60));
    check('在线: 无净值点时不谎称真实',
          registry['equityHint'].hasAttribute('hidden') === true, 'equityHint 仍显示');
  }

  /* B4：实盘就绪（T-401）。只有 fixture 带 live 时才断言，
     这样不影响"离线 6 项"的既有计数。 */
  const live = fixture.live;
  const gateBody = grab('liveGateBody');
  if (live && live.available) {
    const readyKeys = Object.keys(live.live_ready || {});
    const allReady = readyKeys.length > 0 && readyKeys.every(k => live.live_ready[k]);
    contains('实盘: 总状态与 live_ready 一致', gateBody,
             allReady ? '三级授权齐全' : '禁止实盘');
    check('实盘: 状态灯颜色与 live_ready 一致',
          allReady ? gateBody.indexOf('gate-head ok') >= 0
                   : gateBody.indexOf('gate-head bad') >= 0,
          allReady ? '应为绿灯' : '应为红灯');
    /* 缺项必须逐条出现 —— 这是"还差什么"的唯一答案来源。 */
    const missList = [].concat.apply([],
      Object.keys(live.missing || {}).map(k => live.missing[k] || []));
    if (!allReady) {
      check('实盘: 禁止实盘时至少列出一条缺项', missList.length > 0, 'missing 为空');
      missList.forEach(t => contains('实盘: 缺项逐条列出 ' + t, gateBody, t));
    }
    ['第 1 级', '第 2 级', '第 3 级'].forEach(lv => {
      contains('实盘: 三级锁含 ' + lv, gateBody, lv);
    });
    (live.accounts || []).forEach(a => {
      contains('实盘: 账户级显示账户 ID ' + a.account_id, gateBody, a.account_id);
    });
    (live.order_caps || []).forEach(c => {
      const cap = (c.detail || {}).max_notional;
      if (cap !== undefined) {
        contains('实盘: 单笔上限显示 max_notional ' + cap, gateBody,
                 expectMoney2(cap));
      }
    });
    (live.reconciliation || []).forEach(r => {
      contains('实盘: 对账状态 ' + r.account_id, gateBody,
               r.clean ? '四项对账干净' : '对账未清零');
      if (!r.clean) {
        contains('实盘: 对账不干净给出处置提示', gateBody, '必须先处理差异');
      }
    });
    const readyVals = Object.keys(live.live_ready || {}).map(k => live.live_ready[k]);
    if (readyVals.indexOf(false) >= 0) {
      check('实盘: live_ready=false 时不显示"允许实盘"',
            grab('liveGateAt').indexOf('允许实盘') < 0, grab('liveGateAt'));
    }
  } else if (live) {
    contains('实盘: 未接入时降级为灰字提示', gateBody, '实盘开关状态不可用');
    contains('实盘: 未接入时说出原因', gateBody, String(live.reason || '未接入'));
  }

  /* B4：归因日报（T-403）。数据只取 nav-history 最后一个点，不额外请求。 */
  if (navPts.length) {
    const last = navPts[navPts.length - 1];
    const attrBody = grab('attrBody');
    ['交易', '滑点', '手续费', '分红', '资金费', '汇率'].forEach(k => {
      contains('归因: 含拆解项 ' + k, attrBody, k);
    });
    contains('归因: 显示日期 day', grab('attrDay'), String(last.day));
    /* B7 起分红单列：不再让它落进 fx 残差里冒充汇率损益。 */
    const sum = ['trading', 'slippage', 'fee', 'dividend', 'funding', 'fx']
      .reduce((s, k) => s + Number(last[k] || 0), 0);
    const diff = sum - Number(last.net_pnl);
    if (Math.abs(diff) <= 0.02) {
      contains('归因: 闭合时写"闭合"', attrBody, '闭合');
      check('归因: 闭合时不写"未闭合"', attrBody.indexOf('未闭合') < 0, '误报未闭合');
    } else {
      contains('归因: 未闭合时写"未闭合"', attrBody, '未闭合');
    }
    contains('归因: 说明 fx 是残差', attrBody, '以 fx 为残差');
    if (Number(last.fee) > 0) {
      contains('归因: 手续费为正时告警', attrBody, '记账方向可能反了');
    }
    if (Math.abs(Number(last.fx)) > 1e-7) {
      contains('归因: fx 非零时提示核对多币种', attrBody, 'fx 必须精确为 0');
    }
  } else {
    contains('归因: 无日终快照时明说', grab('attrBody'), '尚无日终快照');
    if (fixture.navHistory && fixture.navHistory.reason) {
      contains('归因: 无快照时带出原因', grab('attrBody'), fixture.navHistory.reason);
    }
  }

  /* ===== B7：风险模型 / 灰度 / AI 收件箱 =====
     判据不是"块里有文字"，而是"页面上的数字能对回后端 JSON"，
     以及"后端不给数据时页面会不会编一个绿灯"。 */
  const pf = fixture.portfolio;
  if (pf) {
    /* --- 风险模型 --- */
    const rm = pf.risk_model;
    const rmBody = grab('riskModelBody');
    if (rm && rm.available) {
      contains('风险模型: 写观测数', rmBody, String(rm.n_obs));
      contains('风险模型: 写自由度比', rmBody, Number(rm.dof_ratio).toFixed(2));
      contains('风险模型: 写 ρ 分辨率', rmBody, Number(rm.corr_stderr).toFixed(4));
      contains('风险模型: 写年化因子', rmBody, String(rm.annualization));
      /* 自由度比的分母必须摊开：只说 26.67 等于没说清它在比什么。 */
      const nParams = rm.n_assets + rm.n_assets * (rm.n_assets - 1) / 2;
      contains('风险模型: 写明待估参数个数', rmBody, '待估参数 ' + nParams + ' 个');
      /* 收缩：样本值与收缩后必须同时出现，只给一个等于把人为成分当估计结果。 */
      contains('风险模型: 标出「样本 → 收缩后」', rmBody, '样本值 → 收缩后');
      Object.keys(rm.corr || {}).forEach(k => {
        const pair = k.split('|');
        contains('风险模型: 相关系数对 ' + k, rmBody, pair[0] + ' / ' + pair[1]);
        const rawV = Number((rm.raw_corr || {})[k]);
        if (!isNaN(rawV)) {
          contains('风险模型: 保留收缩前样本值 ' + k, rmBody, rawV.toFixed(3));
        }
        contains('风险模型: 显示收缩后值 ' + k, rmBody, Number(rm.corr[k]).toFixed(3));
      });
      Object.keys(rm.vols || {}).forEach(sym => {
        contains('风险模型: 波动率 ' + sym, rmBody,
                 sym + '</b> σ ' + (Number(rm.vols[sym]) * 100).toFixed(1) + '%');
      });
      (rm.warnings || []).forEach(w => {
        contains('风险模型: 告警原样透出', rmBody, String(w).slice(0, 16));
      });
      /* 年龄与可信度不能互相矛盾：过了 30 天就不许还挂着「估计可用」。 */
      if (Number(rm.age_days) > 30) {
        check('风险模型: 超 30 天时判为不可信',
              rmBody.indexOf('估计不可信') >= 0, '仍显示可用');
        check('风险模型: 超 30 天时右上角标红',
              registry['rmAt'].className.indexOf('p-bad') >= 0,
              registry['rmAt'].className);
      }
      if (rm.trustworthy === false) {
        check('风险模型: trustworthy=false 时判为不可信',
              rmBody.indexOf('估计不可信') >= 0, '仍显示可用');
      }
      /* 收缩幅度超过 ρ 分辨率的对，必须标出来 —— 那是"人为成分大于噪声"。 */
      Object.keys(rm.corr || {}).forEach(k => {
        const rv = Number((rm.raw_corr || {})[k]);
        if (isNaN(rv)) return;
        const d = Number(rm.corr[k]) - rv;
        if (Math.abs(d) > Number(rm.corr_stderr)) {
          contains('风险模型: 超分辨率的收缩标出来 ' + k, rmBody, '超分辨率');
        }
      });
    } else {
      contains('风险模型: 无估计时明说不可用', rmBody, '没有可用的协方差估计');
      check('风险模型: 无估计时不编绿灯',
            registry['rmAt'].className.indexOf('p-ok') < 0,
            registry['rmAt'].className);
    }

    /* --- 灰度 --- */
    const cy = pf.canary;
    const cyBody = grab('canaryBody');
    if (cy && cy.available) {
      (cy.cohorts || []).forEach(c => {
        contains('灰度: 策略 ' + c.strategy_id, cyBody, c.strategy_id);
        contains('灰度: 稳定版 ' + c.stable_version, cyBody, c.stable_version);
        if (c.canary_version) {
          contains('灰度: 灰度版 ' + c.canary_version, cyBody, c.canary_version);
        }
        const wPct = (Number(c.weight) * 100).toFixed(1) + '%';
        contains('灰度: 权重 ' + c.strategy_id, cyBody, wPct);
        const label = { single: '单版本', canary: '灰度中', promoted: '已全量' }[c.state];
        if (label) contains('灰度: 状态标签 ' + c.state, cyBody, label);
      });
      const dualN = (cy.cohorts || []).filter(c => c.state === 'canary').length;
      contains('灰度: 计数与灰度中数量一致', grab('canaryAt'),
               dualN > 0 ? dualN + ' 个灰度中' : (cy.cohorts || []).length + ' 个单版本');
      contains('灰度: 写明自动化只能降权', cyBody, '没有任何分支会提高权重');
    } else {
      contains('灰度: 无路由表时明说', cyBody, '没有返回灰度路由');
    }

    /* --- AI 收件箱 --- */
    const rs = pf.research;
    const rsBody = grab('researchBody');
    if (rs && rs.available) {
      (rs.items || []).forEach(it => {
        contains('收件箱: 含提交编号 ' + it.submission_id, rsBody, it.submission_id);
        contains('收件箱: 含摘要片段', rsBody, String(it.summary).slice(0, 12));
        contains('收件箱: 标出提交人 ' + it.author, rsBody, it.author);
        if (String(it.author).indexOf('ai:') === 0) {
          contains('收件箱: AI 提交打 AI 标', rsBody, '>AI<');
        }
      });
      contains('收件箱: 待复核条数与后端一致', rsBody,
               Number(rs.pending).toFixed(0) + ' 条');
      contains('收件箱: 写明 AI 只有 append 权限', rsBody, '只有 append 权限');
      contains('收件箱: 写明接受不等于上线', rsBody, '不是直接上线');
    } else {
      contains('收件箱: 无数据时明说', rsBody, '没有返回研究收件箱');
    }
  } else {
    /* 没给 portfolio fixture = 端点 404。三块都必须降级成"看不到"，
       且都不许把整页判成离线（另有 5 个请求成功）。 */
    contains('B7: 风险模型降级', grab('riskModelBody'), '不可用');
    contains('B7: 灰度降级', grab('canaryBody'), '不可用');
    contains('B7: 收件箱降级', grab('researchBody'), '不可用');
    check('B7: 单块缺失不把整页判离线',
          grab('liveText').indexOf('实时数据') >= 0, grab('liveText'));
    check('B7: 缺失时右上角不挂绿灯',
          registry['rmAt'].className.indexOf('p-ok') < 0 &&
          registry['canaryAt'].className.indexOf('p-ok') < 0 &&
          registry['researchAt'].className.indexOf('p-ok') < 0,
          '有块被标成可用');
  }

  /* ---------- B9 · 策略排期 ---------- */

  const scBody = grab('scheduleBody');
  const sc = fixture.schedule;
  if (sc && sc.available !== false) {
    contains('排期: 数据日与后端一致', scBody, String(sc.signal_day));
    contains('排期: 日历名与后端一致', scBody,
             (sc.calendar && sc.calendar.name) || '—');
    (sc.entries || []).forEach(function (e) {
      contains('排期: 列出 ' + e.strategy_id, scBody, e.strategy_id);
      if (e.next_due) {
        contains('排期: ' + e.strategy_id + ' 的下次到期与后端一致',
                 scBody, e.next_due);
      }
      if (e.in_doubt) {
        contains('排期: in_doubt 必须写清「可能已经下过单」', scBody, '可能已经下过单');
      }
    });
    if (!(sc.entries || []).length) {
      contains('排期: 没有排期时如实说没有', scBody, '还没有登记任何排期');
    }
    if (sc.signal_day_covered === false) {
      contains('排期: 数据日未校准要写明', scBody, '拒绝运行');
      check('排期: 未校准时不挂绿灯',
            registry['schedAt'].className.indexOf('p-ok') < 0,
            registry['schedAt'].className);
    } else if (!(sc.needs_attention || []).length) {
      check('排期: 健康时右上角挂绿',
            registry['schedAt'].className.indexOf('p-ok') >= 0,
            registry['schedAt'].className);
    }
  } else {
    contains('排期: 不可用时明说', scBody, '不可用');
    check('排期: 不可用时不挂绿灯',
          registry['schedAt'].className.indexOf('p-ok') < 0,
          registry['schedAt'].className);
  }
}

/* 等所有 mock 请求的 setTimeout(0) 跑完，再断言渲染结果。 */
setTimeout(function () {
  try {
    run(fixture ? 'online' : 'offline');
  } catch (e) {
    console.error('ASSERT_CRASH:', e.message);
    process.exit(1);
  }
  const modeLabel = fixture ? '在线（fixture）' : '离线（无运维服务）';
  console.log(`页面探针 · ${modeLabel} · ${pass} 通过 / ${fail} 失败`);
  process.exit(fail === 0 ? 0 : 1);
}, 120);
