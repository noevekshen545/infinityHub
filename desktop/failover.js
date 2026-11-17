/* Перехват роли сервера: чтение настроек, выбор узла, проверка живости.
 *
 * Вынесено из main.js отдельным модулем, чтобы то же самое можно было
 * проверить node-ом без запуска Electron (tools/mesh_failover_parity.py).
 * Здесь нет ни одного обращения к Electron: только fs, path и http.
 *
 * Форматы файлов совпадают с app\mesh_failover.py — расхождение в них
 * означало бы «панель показывает один узел, а оболочка открывает другой».
 */
const fs = require('fs');
const http = require('http');
const path = require('path');

const TRUST_MODES = ['verified', 'any', 'manual'];

function readCfg(root) {
  try {
    const raw = JSON.parse(fs.readFileSync(path.join(root, 'data', 'client_offload.json'), 'utf8'));
    const mesh = ((raw || {}).decentral || {}).mesh || {};
    if (!mesh.failover) return null;
    const trust = TRUST_MODES.includes(mesh.failover_trust) ? mesh.failover_trust : 'verified';
    return { trust, node: String(mesh.failover_node || '').trim() };
  } catch (e) {
    return null;
  }
}

function readCandidates(root) {
  const readFile = (name, key) => {
    try {
      const r = JSON.parse(fs.readFileSync(path.join(root, 'data', name), 'utf8'));
      return (r && typeof r[key] === 'object' && r[key]) || {};
    } catch (e) { return {}; }
  };
  const norm = (u) => {
    const s = String(u || '').trim();
    return /^https?:\/\//i.test(s) ? s.replace(/\/+$/, '') : '';
  };
  const disc = readFile('mesh_discovery.json', 'nodes');
  const peers = readFile('mesh_peers.json', 'peers');
  const out = new Map();
  for (const node of Object.keys(disc)) {
    const rec = disc[node] || {};
    const url = norm(rec.url);
    if (!url) continue;
    out.set(url, { node: String(node).slice(0, 40), url,
      verified: !!rec.verified,
      pinned: Object.prototype.hasOwnProperty.call(peers, node),
      seen: Number(rec.last_seen || 0) });
  }
  for (const node of Object.keys(peers)) {
    const rec = peers[node] || {};
    const url = norm(rec.url);
    if (!url) continue;
    const item = out.get(url) || { node: String(node).slice(0, 40), url,
      verified: true, pinned: true, seen: 0 };
    item.pinned = true;
    item.verified = true;
    out.set(url, item);
  }
  return [...out.values()].sort((a, b) => (Number(b.pinned) - Number(a.pinned))
    || (Number(b.verified) - Number(a.verified)) || (b.seen - a.seen));
}

function pick(cfg, items) {
  if (!cfg) return null;
  if (cfg.trust === 'manual') {
    if (!cfg.node) return null;
    return items.find((c) => c.node === cfg.node || c.url === cfg.node) || null;
  }
  if (cfg.trust === 'verified') {
    return items.find((c) => c.verified || c.pinned) || null;
  }
  return items[0] || null;
}

function probe(url, timeoutMs = 1200) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (ok) => { if (!done) { done = true; resolve(ok); } };
    const req = http.get(url + '/api/health', { timeout: timeoutMs }, (res) => {
      res.resume();
      finish(res.statusCode < 400);
    });
    req.on('error', () => finish(false));
    req.on('timeout', () => { req.destroy(); finish(false); });
  });
}

module.exports = { TRUST_MODES, readCfg, readCandidates, pick, probe };
