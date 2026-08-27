/**
 * tool-search-shim.js — Codex ↔ Antigravity 之间的 tool_search 协议适配层（单文件、零依赖）
 *
 * 背景：Codex 核心对 deferred 工具（mcp__node_repl__* 等）暴露自定义工具类型 tool_search。
 * 核心 router 只接受 ResponseItem::ToolSearchCall { execution:"client", arguments:{query,...} }
 * （SSE item type "tool_search_call"）；经 Antigravity 等非原生代理后，模型的调用以普通
 * function_call(name=tool_search, arguments=JSON字符串) 返回，被核心判为
 * "tool_search handler received unsupported payload"（openai/codex issue #20574），
 * 导致 deferred 工具永远无法挂载。
 *
 * 两种模式：
 *   旁路模式（默认）：对流量零改动，只做日志取证（阶段一）。
 *   改写模式（--rewrite 或 SHIM_REWRITE=1）：按下述规则双向翻译（阶段二）。
 *
 * 改写规则（与 codex-rs 源码逐一对齐，见 codex-rs/tools/src/tool_spec.rs、
 * codex-rs/protocol/src/models.rs、codex-rs/codex-api/src/sse/responses.rs）：
 *   请求方向（Codex → 上游）：
 *     R1 tools[]  {type:"tool_search",execution,description,parameters}
 *              → {type:"function",name:"tool_search",description,parameters}
 *     R2 input[]  {type:"tool_search_call",call_id,arguments(object)}
 *              → {type:"function_call",name:"tool_search",call_id,arguments(JSON字符串)}
 *     R3 input[]  {type:"tool_search_output",call_id,tools,...}
 *              → {type:"function_call_output",call_id,output(JSON字符串)}
 *   响应方向（上游 → Codex）：
 *     W1 SSE response.output_item.done / .added 内
 *              {type:"function_call",name:"tool_search",arguments(string),call_id}
 *              → {type:"tool_search_call",execution:"client",call_id,arguments(object)}
 *        （核心只消费 output_item.done/added；function_call_arguments.delta/.done
 *          被核心显式忽略，无需改动）
 *     W2 非流式 JSON 响应 output[] 数组内同样转换
 *     W3 SSE response.completed 的 response.output[] 内同样转换（一致性，核心不解析该项）
 *
 * 用法：
 *   node tool-search-shim.js [--rewrite] [--port 18045] [--upstream 127.0.0.1:8045] [--log-dir DIR]
 *   --rewrite          开启响应侧改写（W 规则，证据支持的唯一断点）
 *   --rewrite-request  额外开启请求侧改写（R1/R2/R3，默认关：探针证明请求侧无需改写）
 *   环境变量：SHIM_REWRITE=1 SHIM_REWRITE_REQUEST=1 SHIM_PORT SHIM_UPSTREAM SHIM_LOG_DIR
 *
 * 管理端点：GET /__shim/health  GET /__shim/stats
 */

'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');
const { StringDecoder } = require('string_decoder');

// ---------------- 配置 ----------------

function parseArgs(argv) {
  const cfg = {
    port: Number(process.env.SHIM_PORT || 18045),
    upstream: process.env.SHIM_UPSTREAM || '127.0.0.1:8045',
    rewrite: process.env.SHIM_REWRITE === '1',
    rewriteRequest: process.env.SHIM_REWRITE_REQUEST === '1',
    logDir: process.env.SHIM_LOG_DIR || path.join(__dirname, '..', 'shim-logs'),
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--rewrite') cfg.rewrite = true;
    else if (a === '--rewrite-request') cfg.rewriteRequest = true;
    else if (a === '--port') cfg.port = Number(argv[++i]);
    else if (a === '--upstream') cfg.upstream = argv[++i];
    else if (a === '--log-dir') cfg.logDir = argv[++i];
    else if (a === '--help') { console.log('node tool-search-shim.js [--rewrite] [--port N] [--upstream host:port] [--log-dir DIR]'); process.exit(0); }
    else throw new Error(`未知参数: ${a}`);
  }
  const [host, port] = cfg.upstream.split(':');
  cfg.upstreamHost = host;
  cfg.upstreamPort = Number(port || 8045);
  return cfg;
}

// ---------------- 改写规则（纯函数，导出供重放测试） ----------------

/** R1/R2/R3：请求体转换。返回 { body, changes }，无改动时 changes 为空且 body 原样返回。 */
function transformRequestBody(body) {
  const changes = [];
  if (Array.isArray(body.tools)) {
    body.tools = body.tools.map((t) => {
      if (t && t.type === 'tool_search') {
        changes.push('R1 tools:tool_search→function');
        return {
          type: 'function',
          name: 'tool_search',
          description: typeof t.description === 'string' ? t.description : '',
          parameters: t.parameters && typeof t.parameters === 'object'
            ? t.parameters
            : { type: 'object', properties: { query: { type: 'string' }, limit: { type: 'number' } }, required: ['query'] },
        };
      }
      return t;
    });
  }
  if (Array.isArray(body.input)) {
    body.input = body.input.map((it) => {
      if (it && it.type === 'tool_search_call') {
        changes.push('R2 input:tool_search_call→function_call');
        const out = {
          type: 'function_call',
          name: 'tool_search',
          call_id: it.call_id != null ? it.call_id : '',
          arguments: typeof it.arguments === 'string' ? it.arguments : JSON.stringify(it.arguments != null ? it.arguments : {}),
        };
        if (it.status != null) out.status = it.status;
        return out;
      }
      if (it && it.type === 'tool_search_output') {
        changes.push('R3 input:tool_search_output→function_call_output');
        return {
          type: 'function_call_output',
          call_id: it.call_id != null ? it.call_id : '',
          output: JSON.stringify({ status: it.status != null ? it.status : 'completed', tools: Array.isArray(it.tools) ? it.tools : [] }),
        };
      }
      return it;
    });
  }
  return { body, changes };
}

/** W1：单个输出项转换。function_call(tool_search) → tool_search_call。命中返回新对象，否则原样返回。 */
function transformOutputItem(item, changes) {
  if (item && item.type === 'function_call' && item.name === 'tool_search') {
    let args = item.arguments;
    if (typeof args === 'string') {
      try { args = JSON.parse(args); } catch { args = { query: args }; }
    }
    if (args === null || typeof args !== 'object') args = { query: String(args == null ? '' : args) };
    changes.push('W1 resp:function_call(tool_search)→tool_search_call');
    const out = { type: 'tool_search_call', execution: 'client', arguments: args };
    if (item.call_id != null) out.call_id = item.call_id;
    if (item.id != null) out.id = item.id;
    if (item.status != null) out.status = item.status;
    return out;
  }
  return item;
}

/** 遍历任意响应 JSON（SSE data 或非流式 body），转换其中所有输出项。返回 { json, changed } */
function transformResponseJson(json, changes) {
  if (json && json.item && typeof json.item === 'object') {
    json.item = transformOutputItem(json.item, changes); // SSE output_item.done/added
  }
  if (json && Array.isArray(json.output)) {
    json.output = json.output.map((it) => transformOutputItem(it, changes)); // 非流式 body
  }
  if (json && json.response && Array.isArray(json.response.output)) {
    json.response.output = json.response.output.map((it) => transformOutputItem(it, changes)); // response.completed
  }
  return json;
}

const SSE_TARGET_EVENTS = new Set([
  'response.output_item.done',
  'response.output_item.added',
  'response.completed',
]);

/**
 * SSE 事件块转换。输入一个完整事件块（不含结尾空行）的字符串。
 * 非目标事件原样返回；目标事件解析 data JSON → 转换 → 重序列化。
 * 返回 { text, changes }。
 */
function transformSseBlock(block) {
  const changes = [];
  const lines = block.split(/\r?\n/);
  let eventType = null;
  const dataIdx = [];
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].startsWith('event:')) eventType = lines[i].slice(6).trim();
    else if (lines[i].startsWith('data:')) dataIdx.push(i);
  }
  if (dataIdx.length === 0) return { text: block, changes };
  const dataRaw = dataIdx.map((i) => lines[i].slice(5).replace(/^ /, '')).join('\n');
  let kind = eventType;
  let json = null;
  try { json = JSON.parse(dataRaw); } catch { json = null; }
  if (kind === null && json && typeof json.type === 'string') kind = json.type;
  if (kind === null || !SSE_TARGET_EVENTS.has(kind) || json === null) return { text: block, changes };
  json = transformResponseJson(json, changes);
  if (changes.length === 0) return { text: block, changes };
  // 重序列化：保留 event 行，data 合并为单行
  const outLines = [];
  for (let i = 0; i < lines.length; i++) {
    if (dataIdx.includes(i)) {
      if (i === dataIdx[0]) outLines.push('data: ' + JSON.stringify(json));
      continue;
    }
    outLines.push(lines[i]);
  }
  return { text: outLines.join('\r\n'), changes };
}

// ---------------- SSE 增量切分器 ----------------

class SseSplitter {
  constructor(onBlock) {
    this.decoder = new StringDecoder('utf8');
    this.buf = '';
    this.onBlock = onBlock; // (blockText) => outText
  }
  /** chunk(Buffer) → 可立即写出的字符串数组 */
  push(chunk) {
    this.buf += this.decoder.write(chunk);
    const out = [];
    // 事件边界：空行（\n\n 或 \r\n\r\n 或混合）。边界原样保留——丢失空行会让所有事件合并、客户端永远等不到事件结束
    for (;;) {
      const m = this.buf.match(/\r?\n\r?\n/);
      if (!m) break;
      const block = this.buf.slice(0, m.index);
      const sep = m[0];
      this.buf = this.buf.slice(m.index + sep.length);
      out.push(this.onBlock(block) + sep);
    }
    return out;
  }
  end() {
    this.buf += this.decoder.end();
    if (this.buf.length > 0) {
      const rest = this.buf;
      this.buf = '';
      return [this.onBlock(rest)];
    }
    return [];
  }
}

// ---------------- 代理服务器 ----------------

function main() {
  const cfg = parseArgs(process.argv);
  const capDir = path.join(cfg.logDir, 'captures');
  fs.mkdirSync(capDir, { recursive: true });
  const trafficLog = path.join(cfg.logDir, 'traffic.jsonl');

  const stats = { requests: 0, toolSearchRequests: 0, rewritten: 0, errors: 0, startedAt: new Date().toISOString() };

  function logLine(obj) {
    fs.appendFile(trafficLog, JSON.stringify(obj) + '\n', () => {});
  }

  const server = http.createServer((req, res) => {
    const id = ++stats.requests;
    const ts = new Date().toISOString();

    // 管理端点
    if (req.url === '/__shim/health') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true, rewrite: cfg.rewrite, upstream: cfg.upstream }));
      return;
    }
    if (req.url === '/__shim/stats') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(stats));
      return;
    }

    // 1. 收完整请求体（请求体有限，可缓冲；响应体绝不整体缓冲）
    const reqChunks = [];
    let reqBytes = 0;
    req.on('data', (c) => { reqChunks.push(c); reqBytes += c.length; });
    req.on('error', () => { stats.errors++; res.destroy(); });
    req.on('end', () => {
      const reqBody = Buffer.concat(reqChunks);
      const reqText = reqBody.toString('utf8');
      const isResponses = req.method === 'POST' && /\/responses$/.test(req.url.split('?')[0]);
      const toolSearchRelated = isResponses && reqText.includes('tool_search');

      // 请求侧摘要（tools 结构）
      let bodyJson = null;
      let toolsSummary = null;
      if (isResponses) {
        try {
          bodyJson = JSON.parse(reqText);
          if (Array.isArray(bodyJson.tools)) {
            toolsSummary = bodyJson.tools.map((t) => `${t && t.type}${t && t.name ? ':' + t.name : ''}`).join(',');
          }
        } catch { /* 非 JSON，原样转发 */ }
      }

      // 2. 改写模式：转换请求体（仅 --rewrite-request；探针证明 Antigravity 请求侧无需改写）
      let outBody = reqBody;
      let reqChanges = [];
      if (cfg.rewrite && cfg.rewriteRequest && isResponses && bodyJson) {
        const r = transformRequestBody(bodyJson);
        if (r.changes.length > 0) {
          reqChanges = r.changes;
          outBody = Buffer.from(JSON.stringify(r.body), 'utf8');
        }
      }

      // 3. 转发到上游
      const headers = Object.assign({}, req.headers);
      headers.host = `${cfg.upstreamHost}:${cfg.upstreamPort}`;
      headers['content-length'] = outBody.length;
      if (cfg.rewrite) headers['accept-encoding'] = 'identity'; // 改写需要明文
      delete headers.connection;

      const upReq = http.request({
        host: cfg.upstreamHost,
        port: cfg.upstreamPort,
        path: req.url,
        method: req.method,
        headers,
        timeout: 0,
      }, (upRes) => {
        const contentType = String(upRes.headers['content-type'] || '');
        const isSse = contentType.includes('text/event-stream');
        const doRespRewrite = cfg.rewrite && isResponses && (isSse || contentType.includes('json'));
        const captureFull = toolSearchRelated || (upRes.statusCode >= 400);
        const respChanges = [];
        const capChunks = captureFull ? [] : null;
        let respBytes = 0;

        const respHeaders = Object.assign({}, upRes.headers);
        if (doRespRewrite) {
          delete respHeaders['content-length'];
          delete respHeaders['content-encoding'];
          delete respHeaders['transfer-encoding']; // 由 Node 重新分块
        }
        res.writeHead(upRes.statusCode, respHeaders);

        const finishLog = (err) => {
          const entry = {
            id, ts, method: req.method, path: req.url, reqBytes,
            toolSearch: toolSearchRelated, tools: toolsSummary,
            stream: isSse, rewrite: cfg.rewrite,
            reqChanges: reqChanges.length ? reqChanges : undefined,
            respChanges: respChanges.length ? [...new Set(respChanges)] : undefined,
            status: upRes.statusCode, respBytes,
            ms: Date.now() - new Date(ts).getTime(),
            err: err ? String(err) : undefined,
          };
          logLine(entry);
          if (err) stats.errors++;
          if (captureFull) {
            const base = path.join(capDir, `${String(id).padStart(5, '0')}`);
            fs.writeFile(`${base}-request.json`, reqBody, () => {});
            fs.writeFile(`${base}-response.raw`, Buffer.concat(capChunks), () => {});
          }
        };

        if (!doRespRewrite) {
          // 纯透传（旁路模式 / 非目标响应）：逐块立即转发，仅旁路取样
          upRes.on('data', (c) => {
            respBytes += c.length;
            if (capChunks) capChunks.push(c);
            res.write(c);
          });
          upRes.on('end', () => { res.end(); finishLog(); });
          upRes.on('error', (e) => { res.destroy(); finishLog(e); });
          return;
        }

        if (isSse) {
          // SSE：按事件块增量转换，事件完整即发出，绝不整流缓冲
          const splitter = new SseSplitter((block) => {
            const r = transformSseBlock(block);
            for (const ch of r.changes) respChanges.push(ch);
            return r.text;
          });
          upRes.on('data', (c) => {
            respBytes += c.length;
            if (capChunks) capChunks.push(c);
            for (const out of splitter.push(c)) res.write(out);
          });
          upRes.on('end', () => {
            for (const out of splitter.end()) res.write(out);
            res.end(); finishLog();
          });
          upRes.on('error', (e) => { res.destroy(); finishLog(e); });
        } else {
          // 非流式 JSON：较小，整体转换（仍不是 SSE，无流式要求）
          const chunks = [];
          upRes.on('data', (c) => { chunks.push(c); respBytes += c.length; if (capChunks) capChunks.push(c); });
          upRes.on('end', () => {
            let out = Buffer.concat(chunks);
            try {
              const json = JSON.parse(out.toString('utf8'));
              const before = respChanges.length;
              transformResponseJson(json, respChanges);
              if (respChanges.length > before) out = Buffer.from(JSON.stringify(json), 'utf8');
            } catch { /* 非 JSON 原样 */ }
            res.end(out); finishLog();
          });
          upRes.on('error', (e) => { res.destroy(); finishLog(e); });
        }
      });

      upReq.on('error', (e) => {
        stats.errors++;
        logLine({ id, ts, method: req.method, path: req.url, err: 'upstream: ' + String(e), rewrite: cfg.rewrite });
        if (!res.headersSent) res.writeHead(502, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: { message: `shim upstream error: ${e.message}`, type: 'shim_upstream_error' } }));
      });
      upReq.end(outBody);
    });
  });

  server.setTimeout(0); // SSE 长连接不限时
  server.keepAliveTimeout = 0;
  server.listen(cfg.port, '127.0.0.1', () => {
    console.log(`[shim] listening http://127.0.0.1:${cfg.port} -> upstream http://${cfg.upstream}`);
    console.log(`[shim] mode: ${cfg.rewrite ? `REWRITE（响应侧改写开 / 请求侧改写${cfg.rewriteRequest ? '开' : '关'}）` : 'BYPASS（旁路，零改动）'}`);
    console.log(`[shim] logs: ${cfg.logDir}`);
  });
}

if (require.main === module) {
  main();
}

module.exports = {
  transformRequestBody,
  transformOutputItem,
  transformResponseJson,
  transformSseBlock,
  SseSplitter,
  parseArgs,
};
