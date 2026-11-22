#!/usr/bin/env node
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const windows = os.platform() === 'win32';
const fleetConfig = JSON.parse(await fs.readFile(path.join(root, 'fleet.json'), 'utf8'));
const configuredState = windows ? 'D:\\AgentWork\\fleet-state' : fleetConfig.state_dir;
export const state = (process.env.FLEET_STATE_DIR || configuredState).replace(/^~\//, os.homedir() + '/');
const base = 'http://127.0.0.1:3773';
const read = async p => JSON.parse(await fs.readFile(p, 'utf8'));
const privateWrite = async (name, value) => fs.writeFile(path.join(state, 'private', name), JSON.stringify(value, null, 2) + '\n', {mode: 0o600});
// T3 binds a websocket ticket to its use; servers older than that have only the bare path and answer 404.
export const ticketPaths = ['/api/auth/websocket-ticket/rpc', '/api/auth/websocket-ticket'];
// Protocol 2 servers answer 426 to a socket that does not name the orchestration protocol; older ones ignore it.
export const socketUrl = ticket => `${base.replace(/^http/, 'ws')}/ws?wsTicket=${encodeURIComponent(ticket)}&orchestrationProtocol=2`;

export async function connect() {
  const auth = await read(path.join(state, 'private/t3-session.json'));
  let response;
  for (const ticketPath of ticketPaths) {
    response = await fetch(base + ticketPath, {method: 'POST', headers: {Authorization: 'Bearer ' + auth.access_token}});
    if (response.status !== 404) break;
  }
  if (!response.ok) throw new Error(`T3 ticket failed: ${response.status}`);
  const {ticket} = await response.json();
  const ws = new WebSocket(socketUrl(ticket));
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, {once: true});
    ws.addEventListener('error', reject, {once: true});
  });
  let next = 0;
  const pending = new Map();
  ws.addEventListener('message', event => {
    const parsed = JSON.parse(String(event.data));
    for (const message of Array.isArray(parsed) ? parsed : [parsed]) {
      if (message._tag === 'Ping') { ws.send(JSON.stringify({_tag: 'Pong'})); continue; }
      if (message._tag !== 'Exit') continue;
      const waiter = pending.get(String(message.requestId));
      if (!waiter) continue;
      clearTimeout(waiter.timer);
      pending.delete(String(message.requestId));
      if (message.exit._tag === 'Success') waiter.resolve(message.exit.value);
      else waiter.reject(new Error('T3 RPC failed: ' + JSON.stringify(message.exit)));
    }
  });
  return {
    close: () => ws.close(),
    rpc(tag, payload = {}) {
      const id = String(++next);
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { pending.delete(id); reject(new Error(`T3 RPC timed out: ${tag}`)); }, 15000);
        pending.set(id, {resolve, reject, timer});
        ws.send(JSON.stringify({_tag: 'Request', id, tag, payload, headers: []}));
      });
    }
  };
}

// Protocol 2 servers dropped server.getSettings; server.getConfig carries the same settings.
export async function getSettings(client) {
  try {
    return await client.rpc('server.getSettings');
  } catch (error) {
    if (!error.message.includes('Unknown request tag')) throw error;
    return (await client.rpc('server.getConfig')).settings;
  }
}

async function main() {
  const client = await connect();
  try {
    const settings = await getSettings(client);
    if (process.argv[2] === 'set-default') {
      const selected = process.argv[process.argv.indexOf('--profile') + 1];
      const config = await read(path.join(root, 'fleet.json'));
      const p = config.profiles[selected];
      if (!p?.managed || !settings.providerInstances[p.t3_instance]) throw new Error('Select an installed fleet profile with --profile NAME');
      const desired = {instanceId: p.t3_instance, model: p.model.replace('[1m]', ''), options: [{id: p.driver === 'codex' ? 'reasoningEffort' : 'effort', value: p.effort}, ...(p.model.includes('[1m]') ? [{id: 'contextWindow', value: '1m'}] : [])]};
      console.log(JSON.stringify({newThreadDefault: desired, apply: process.argv.includes('--apply')}));
      if (!process.argv.includes('--apply') || isDeepStrictEqual(settings.defaultModelSelection, desired)) return;
      if (!isDeepStrictEqual(await getSettings(client), settings)) throw new Error('Settings changed during preparation; retry');
      await privateWrite('t3-before-default-' + Date.now() + '.json', settings);
      const result = await client.rpc('server.updateSettings', {patch: {defaultModelSelection: desired}});
      if (!isDeepStrictEqual(result.providerInstances, settings.providerInstances)) throw new Error('Provider identities changed unexpectedly; inspect before continuing');
      console.log('Updated the default for new threads; existing provider identities preserved.');
      return;
    }
    if (process.argv[2] !== 'add-profiles') {
      await privateWrite('t3-settings-read.json', settings);
      console.log(JSON.stringify({providers: Object.keys(settings.providerInstances), usageSources: Object.keys(settings.usageLimitSources ?? {})}));
      return;
    }
    const baseline = await read(path.join(state, 'baseline.json'));
    const runtime = await read(path.join(os.homedir(), '.t3/userdata/server-runtime.json'));
    if (runtime.pid !== baseline.t3_runtime.pid || runtime.startedAt !== baseline.t3_runtime.startedAt) throw new Error('T3 runtime changed; inspect it before applying');
    const config = await read(path.join(root, 'fleet.json'));
    const instances = {...settings.providerInstances};
    const sources = {...settings.usageLimitSources};
    const additions = [];
    for (const [name, p] of Object.entries(config.profiles)) {
      if (!p.managed) continue;
      const home = windows ? path.join(state, 'profiles', name) : p.home.replace(/^~\//, os.homedir() + '/');
      await fs.access(path.join(home, '.fleet-profile.json'));
      const runtime = windows ? await read(path.join(state, 'windows-runtime.json')) : undefined;
      const binaryPath = runtime?.[p.driver]?.binary;
      if (binaryPath) await fs.access(binaryPath);
      const desired = {driver: p.driver === 'claude' ? 'claudeAgent' : 'codex', displayName: name === 'fleet-secondary' ? 'Fleet Claude Secondary' : p.driver === 'claude' ? 'Fleet Claude Personal' : 'Fleet Codex Personal', enabled: true, config: {homePath: home, ...(binaryPath ? {binaryPath} : {})}};
      if (windows) {
        const identity = p.gateway ?? 'personal';
        const gateway = identity === 'personal' ? config.gateway : config.additional_gateways[identity];
        const key = (await read(path.join(state, 'gateway-keys', identity + '.json'))).api_key;
        desired.environment = p.driver === 'claude' ? [
          {name: 'ANTHROPIC_BASE_URL', value: gateway.url, sensitive: false},
          {name: 'ANTHROPIC_AUTH_TOKEN', value: key, sensitive: true},
          {name: 'ANTHROPIC_API_KEY', value: '', sensitive: false},
          ...(p.identity === 'personal' ? [{name: 'CODEX_HOME', value: path.join(os.homedir(), '.codex'), sensitive: false}] : [])
        ] : [
          {name: 'OPENAI_BASE_URL', value: gateway.url + '/v1', sensitive: false},
          {name: 'OPENAI_API_KEY', value: key, sensitive: true}
        ];
      }
      if (instances[p.t3_instance]) {
        const previous = instances[p.t3_instance];
        const comparable = structuredClone(desired);
        for (const variable of comparable.environment ?? []) {
          const saved = previous.environment?.find(v => v.name === variable.name);
          if (saved?.valueRedacted && variable.sensitive) Object.assign(variable, {value: saved.value, valueRedacted: true});
        }
        if (!isDeepStrictEqual(previous, comparable)) throw new Error(`Preserving existing instance ${p.t3_instance}; review changes manually`);
      } else {instances[p.t3_instance] = desired; additions.push(p.t3_instance);}
    }
    // Management credentials stay on the gateway host. Windows uses the central
    // devbox quota view and receives only per-identity inference client keys.
    for (const [name, gateway] of Object.entries(windows ? {} : {personal: config.gateway, ...config.additional_gateways})) {
      const id = 'fleet_' + name;
      if (sources[id]) continue;
      const privatePath = gateway.runtime_dir.replace(/^~\//, os.homedir() + '/') + '/private/client.json';
      const keys = await read(privatePath);
      sources[id] = {kind: 'cliproxy', label: 'Fleet ' + name, url: gateway.url, managementKey: keys.management_key, enabled: true};
    }
    console.log(JSON.stringify({newProviders: additions, newUsageSources: Object.keys(sources).filter(k => !settings.usageLimitSources?.[k]), apply: process.argv.includes('--apply')}));
    if (!process.argv.includes('--apply')) return;
    const fresh = await getSettings(client);
    if (!isDeepStrictEqual(fresh, settings)) throw new Error('T3 settings changed during preparation; retry from a fresh read');
    await privateWrite('t3-before-additions.json', settings);
    const result = await client.rpc('server.updateSettings', {patch: {providerInstances: instances, usageLimitSources: sources}});
    for (const [id, previous] of Object.entries(settings.providerInstances)) {
      if (!isDeepStrictEqual(result.providerInstances[id], previous)) throw new Error(`Existing instance changed unexpectedly: ${id}. Inspect before further actions.`);
    }
    await privateWrite('t3-after-additions.json', result);
    console.log('Added fleet settings. Existing provider entries preserved exactly.');
  } finally {client.close();}
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {console.error(error.message); process.exitCode = 1;});
}
