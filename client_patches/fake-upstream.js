// fake-upstream.js — 本地假上游：返回含 function_call(tool_search) 的 SSE 流/非流式 JSON
// 仅用于 shim 的确定性端到端验证，不打真实 Antigravity。
'use strict';
const http = require('http');

const CALL_ID = 'call_fake_001';

function sseEvents() {
  const itemShell = { type: 'response.output_item.added', output_index: 0, item: { type: 'function_call', name: 'tool_search', arguments: '', call_id: CALL_ID, status: 'in_progress' } };
  const itemDone = { type: 'response.output_item.done', output_index: 0, item: { type: 'function_call', name: 'tool_search', arguments: '{"query":"node_repl js"}', call_id: CALL_ID, status: 'completed' } };
  const completed = { type: 'response.completed', response: { id: 'resp_fake', status: 'completed', output: [itemDone.item], usage: { input_tokens: 10, output_tokens: 5 } } };
  return [
    ['response.created', { type: 'response.created', response: { id: 'resp_fake', status: 'in_progress' } }],
    ['response.output_item.added', itemShell],
    ['response.function_call_arguments.delta', { type: 'response.function_call_arguments.delta', item_id: 'fc_1', delta: '{"query":"node_re' }],
    ['response.function_call_arguments.delta', { type: 'response.function_call_arguments.delta', item_id: 'fc_1', delta: 'pl js"}' }],
    ['response.function_call_arguments.done', { type: 'response.function_call_arguments.done', item_id: 'fc_1', arguments: '{"query":"node_repl js"}' }],
    ['response.output_item.done', itemDone],
    ['response.completed', completed],
  ];
}

const server = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    let parsed = {};
    try { parsed = JSON.parse(body); } catch {}
    if (parsed.stream === false) {
      // 非流式：返回 output[] 含 function_call(tool_search)
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({
        id: 'resp_fake', object: 'response', status: 'completed',
        output: [{ type: 'function_call', name: 'tool_search', arguments: '{"query":"node_repl js"}', call_id: CALL_ID, status: 'completed' }],
        usage: { input_tokens: 10, output_tokens: 5 },
      }));
      return;
    }
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
    const events = sseEvents();
    let i = 0;
    const tick = () => {
      if (i >= events.length) { res.end(); return; }
      const [kind, data] = events[i++];
      // 故意把单个事件拆成两个 TCP chunk 写，验证 shim 增量切分
      const text = `event: ${kind}\r\ndata: ${JSON.stringify(data)}\r\n\r\n`;
      const half = Math.floor(text.length / 2);
      res.write(text.slice(0, half));
      setTimeout(() => { res.write(text.slice(half)); setTimeout(tick, 10); }, 10);
    };
    tick();
  });
});
server.listen(18099, '127.0.0.1', () => console.log('fake upstream on 18099'));
