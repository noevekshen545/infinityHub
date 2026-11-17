const _m = { exports: {} };
const url = '/node_modules/eventemitter3/index.js';
const code = await fetch(url).then(r => r.text());
(new Function('module', 'exports', 'require', code))(_m, _m.exports, (id) => { throw new Error('require('+id+') not available'); });
export default _m.exports;
