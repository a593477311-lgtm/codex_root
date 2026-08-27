/**
 * shim-replay-test.js — tool-search-shim 的单元测试 + 真实流量重放测试
 *
 * 用法：
 *   node shim-replay-test.js            # 单元测试 + 重放 shim-logs/captures 里的真实流量
 *
 * 单元测试的线格式断言与 codex-rs 源码逐一对齐：
 *   - 请求 tools 定义：codex-rs/tools/src/tool_spec_tests.rs (tool_search_tool_spec_serializes_expected_wire_shape)
 *   - tool_search_call：codex-rs/protocol/src/models.rs (tool_search_call_roundtrips)
 *   - tool_search_output：codex-rs/protocol/src/models.rs (tool_search_output_roundtrips)
 *
 * 重放测试：对阶段一（旁路模式）录下的 request/response 对，验证：
 *   1. 改写后请求中不再含 type:"tool_search" 的工具 / type:"tool_search_call|tool_search_output" 的输入项；
 *   2. 改写后响应里原 function_call(name=tool_search) 变成 tool_search_call 且 arguments 为对象；
 *   3. 与 tool_search 无关的 SSE 事件块字节不变。
 */

'use strict';

const fs = require('fs');
const path = require('path');
const {
  transformRequestBody,
  transformOutputItem,
  transformSseBlock,
  SseSplitter,
} = require('./tool-search-shim.js');

let passed = 0, failed = 0;
function ok(cond, name, detail) {
  if (cond) { passed++; console.log(`[PASS] ${name}`); }
  else { failed++; console.log(`[FAIL] ${name}${detail ? '  ' + detail : ''}`); }
}

// ---------------- 单元测试 ----------------

// R1：tools 数组 tool_search → function（线格式对齐 tool_spec_tests.rs）
{
  const toolSearchDef = {
    type: 'tool_search',
    execution: 'client',
    description: '# Tool discovery\n\nSearches over deferred tool metadata...',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'Search query for deferred tools.' },
        limit: { type: 'number', description: 'Maximum number of tools to return.' },
      },
      required: ['query'],
      additionalProperties: false,
    },
  };
  const body = { model: 'm', tools: [toolSearchDef, { type: 'function', name: 'shell' }], input: [] };
  const { body: out, changes } = transformRequestBody(JSON.parse(JSON.stringify(body)));
  ok(changes.length === 1 && changes[0].startsWith('R1'), 'R1 记录变更');
  ok(out.tools[0].type === 'function' && out.tools[0].name === 'tool_search', 'R1 tool_search→function');
  ok(out.tools[0].parameters.required.includes('query'), 'R1 保留 parameters');
  ok(out.tools[1].type === 'function' && out.tools[1].name === 'shell', 'R1 不动标准 function');
  ok(!('execution' in out.tools[0]), 'R1 去掉 execution 字段');
}

// R2：input 历史 tool_search_call → function_call（arguments 变字符串）
{
  const body = {
    input: [
      { type: 'tool_search_call', call_id: 'search-1', execution: 'client', arguments: { query: 'node_repl js', limit: 1 } },
      { type: 'message', role: 'user', content: [] },
    ],
  };
  const { body: out, changes } = transformRequestBody(JSON.parse(JSON.stringify(body)));
  ok(changes.length === 1 && changes[0].startsWith('R2'), 'R2 记录变更');
  const it = out.input[0];
  ok(it.type === 'function_call' && it.name === 'tool_search' && it.call_id === 'search-1', 'R2 形状');
  ok(typeof it.arguments === 'string' && JSON.parse(it.arguments).query === 'node_repl js', 'R2 arguments 字符串化');
  ok(out.input[1].type === 'message', 'R2 不动 message');
}

// R3：input 历史 tool_search_output → function_call_output（output 为字符串）
{
  const body = {
    input: [{
      type: 'tool_search_output', call_id: 'search-1', status: 'completed', execution: 'client',
      tools: [{ type: 'function', name: 'mcp__node_repl__js', defer_loading: true, parameters: { type: 'object' } }],
    }],
  };
  const { body: out, changes } = transformRequestBody(JSON.parse(JSON.stringify(body)));
  ok(changes.length === 1 && changes[0].startsWith('R3'), 'R3 记录变更');
  const it = out.input[0];
  ok(it.type === 'function_call_output' && it.call_id === 'search-1', 'R3 形状');
  const payload = JSON.parse(it.output);
  ok(typeof it.output === 'string' && payload.tools[0].name === 'mcp__node_repl__js', 'R3 tools 进 output 字符串');
}

// W1：响应 function_call(tool_search) → tool_search_call（对齐 models.rs roundtrip）
{
  const changes = [];
  const item = { type: 'function_call', name: 'tool_search', arguments: '{"query":"node_repl js"}', call_id: 'call_e334', status: 'completed' };
  const out = transformOutputItem(JSON.parse(JSON.stringify(item)), changes);
  ok(out.type === 'tool_search_call' && out.execution === 'client', 'W1 type/execution');
  ok(out.call_id === 'call_e334' && out.status === 'completed', 'W1 保留 call_id/status');
  ok(typeof out.arguments === 'object' && out.arguments.query === 'node_repl js', 'W1 arguments 对象化');
  ok(!('name' in out), 'W1 去掉 name');
  ok(changes.length === 1, 'W1 记录变更');
  // 幂等：tool_search_call 不再被转换
  const out2 = transformOutputItem(JSON.parse(JSON.stringify(out)), []);
  ok(out2.type === 'tool_search_call', 'W1 幂等');
}

// W1 容错：arguments 非 JSON 字符串 → 包成 {query: raw}
{
  const out = transformOutputItem({ type: 'function_call', name: 'tool_search', arguments: 'not json', call_id: 'c1' }, []);
  ok(out.arguments.query === 'not json', 'W1 非法 JSON 容错');
}

// W1 不动其它 function_call
{
  const changes = [];
  const item = { type: 'function_call', name: 'mcp__node_repl__js', arguments: '{"code":"1"}', call_id: 'c2' };
  const out = transformOutputItem(JSON.parse(JSON.stringify(item)), changes);
  ok(out.type === 'function_call' && out.name === 'mcp__node_repl__js' && changes.length === 0, 'W1 不动 mcp 工具调用');
}

// SSE：目标事件转换 + 非目标事件字节不变
{
  const doneBlock = 'event: response.output_item.done\r\ndata: {"type":"response.output_item.done","item":{"type":"function_call","name":"tool_search","arguments":"{\\"query\\":\\"js\\"}","call_id":"c9"}}';
  const r1 = transformSseBlock(doneBlock);
  ok(r1.changes.length === 1, 'SSE done 块命中');
  const parsed = JSON.parse(r1.text.split(/\r?\n/).find((l) => l.startsWith('data:')).slice(5).trim());
  ok(parsed.item.type === 'tool_search_call' && parsed.item.arguments.query === 'js', 'SSE done 块转换正确');

  const deltaBlock = 'event: response.function_call_arguments.delta\r\ndata: {"type":"response.function_call_arguments.delta","delta":"{\\"qu"}';
  const r2 = transformSseBlock(deltaBlock);
  ok(r2.text === deltaBlock && r2.changes.length === 0, 'SSE delta 块字节不变');
}

// SSE splitter：跨 chunk 切分 + UTF-8 多字节安全
{
  const blocks = [];
  const sp = new SseSplitter((b) => { blocks.push(b); return b; });
  const text = 'event: response.output_text.delta\r\ndata: {"delta":"中文' ;
  const buf = Buffer.from(text + '片段"}\r\n\r\nevent: x\r\ndata: y\r\n\r\n', 'utf8');
  // 按字节切断（可能切在多字节字符中间）
  let outs = [];
  for (let i = 0; i < buf.length; i += 7) outs = outs.concat(sp.push(buf.subarray(i, Math.min(i + 7, buf.length))));
  outs = outs.concat(sp.end());
  ok(blocks.length === 2, 'splitter 切出 2 个事件块');
  ok(outs.join('').includes('中文片段'), 'splitter UTF-8 安全');
}

// SSE splitter：事件边界的空行必须原样保留（回归测试——丢失空行会导致客户端报 stream closed before response.completed）
{
  const sp = new SseSplitter((b) => b);
  const wire = 'event: a\r\ndata: 1\r\n\r\nevent: b\r\ndata: 2\n\nevent: c\r\ndata: 3\r\n\r\n';
  let outs = [];
  for (let i = 0; i < wire.length; i += 5) outs = outs.concat(sp.push(Buffer.from(wire.slice(i, i + 5))));
  outs = outs.concat(sp.end());
  ok(outs.join('') === wire, 'splitter 非目标事件输出与输入逐字节一致（含空行边界）');

  const sp2 = new SseSplitter((b) => b.toUpperCase());
  const wire2 = 'data: x\n\n';
  const out2 = sp2.push(Buffer.from(wire2)).concat(sp2.end()).join('');
  ok(out2 === 'DATA: X\n\n', 'splitter 目标事件改写后仍保留空行边界');
}

// ---------------- 重放测试（阶段一录制的真实流量） ----------------

const capDir = path.join(__dirname, '..', 'shim-logs', 'captures');
if (fs.existsSync(capDir)) {
  const reqs = fs.readdirSync(capDir).filter((f) => f.endsWith('-request.json')).sort();
  let replayed = 0;
  for (const reqFile of reqs) {
    const base = reqFile.replace(/-request\.json$/, '');
    const respFile = path.join(capDir, `${base}-response.raw`);
    const reqRaw = fs.readFileSync(path.join(capDir, reqFile));
    // 请求重放
    let body;
    try { body = JSON.parse(reqRaw.toString('utf8')); } catch { continue; }
    const { body: outBody } = transformRequestBody(JSON.parse(JSON.stringify(body)));
    const tools = Array.isArray(outBody.tools) ? outBody.tools : [];
    const inputs = Array.isArray(outBody.input) ? outBody.input : [];
    ok(!tools.some((t) => t && t.type === 'tool_search'), `重放 ${base}: 改写后 tools 无 tool_search 自定义类型`);
    ok(!inputs.some((i) => i && (i.type === 'tool_search_call' || i.type === 'tool_search_output')), `重放 ${base}: 改写后 input 无 tool_search_* 项`);
    // 响应重放（SSE 或 JSON）
    if (fs.existsSync(respFile)) {
      const respRaw = fs.readFileSync(respFile).toString('utf8');
      if (respRaw.trimStart().startsWith('{')) {
        // 非流式 JSON body：走 transformResponseJson 路径
        const { transformResponseJson } = require('./tool-search-shim.js');
        let json;
        try { json = JSON.parse(respRaw); } catch { json = null; }
        if (json) {
          const hadFnCallTs = JSON.stringify(json).includes('"function_call"') && JSON.stringify(json).includes('"tool_search"');
          const changes = [];
          transformResponseJson(json, changes);
          if (hadFnCallTs) ok(changes.length > 0, `重放 ${base}: 非流式 function_call(tool_search) 被转换`);
          const hasTsc = JSON.stringify(json).includes('"tool_search_call"');
          if (changes.length > 0) ok(hasTsc, `重放 ${base}: 转换产物含 tool_search_call`);
        }
      } else {
        // SSE：按事件块重放
        const evtBlocks = respRaw.split(/\r?\n\r?\n/).filter(Boolean);
        let sawFnCall = false, sawTsc = false, unrelatedIntact = true;
        for (const blk of evtBlocks) {
          if (blk.includes('"function_call"') && blk.includes('"tool_search"')) sawFnCall = true;
          const r = transformSseBlock(blk);
          if (r.changes.length > 0) sawTsc = true;
          if (r.changes.length === 0 && r.text !== blk) unrelatedIntact = false;
        }
        if (sawFnCall) {
          ok(sawTsc, `重放 ${base}: 原始 function_call(tool_search) 被转换`);
        }
        ok(unrelatedIntact, `重放 ${base}: 未命中事件块字节不变`);
      }
    }
    replayed++;
  }
  console.log(`重放完成：${replayed} 个请求`);
} else {
  console.log('（无 captures 目录，跳过重放测试）');
}

console.log(`\n结果：${passed} 通过 / ${failed} 失败`);
process.exit(failed === 0 ? 0 : 1);
