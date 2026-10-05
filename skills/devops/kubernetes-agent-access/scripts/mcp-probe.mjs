// Drive mcp-server-kubernetes over stdio with a given kubeconfig, outside Hermes.
//   KUBECONFIG_PATH=~/.kube/hermes-readonly.yaml node mcp-probe.mjs
// Expect: pods listed, secrets Forbidden. Waits for the initialize reply before calling tools.
import { spawn } from 'child_process';
const env = { PATH: process.env.PATH, HOME: process.env.HOME,
  KUBECONFIG_PATH: process.env.KUBECONFIG_PATH || process.env.HOME + '/.kube/hermes-readonly.yaml' };
const p = spawn('npx', ['-y', 'mcp-server-kubernetes'], { env });
const send = o => p.stdin.write(JSON.stringify(o) + '\n');
const call = (id, resourceType) => send({ jsonrpc: '2.0', id, method: 'tools/call',
  params: { name: 'kubectl_get', arguments: { resourceType, namespace: 'default', output: 'name' } } });
let buf = '';
p.stdout.on('data', d => { buf += d; let i;
  while ((i = buf.indexOf('\n')) >= 0) { const l = buf.slice(0, i); buf = buf.slice(i + 1);
    let m; try { m = JSON.parse(l); } catch { continue; }
    if (m.id === 1) { send({ jsonrpc: '2.0', method: 'notifications/initialized' }); call(2, 'pods'); }
    else if (m.id === 2) { console.log('pods:', JSON.stringify(m.result ?? m.error).slice(0, 300)); call(3, 'secrets'); }
    else if (m.id === 3) { console.log('secrets:', JSON.stringify(m.result ?? m.error).slice(0, 300)); p.kill(); } } });
send({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'probe', version: '0' } } });
setTimeout(() => p.kill(), 60000);
