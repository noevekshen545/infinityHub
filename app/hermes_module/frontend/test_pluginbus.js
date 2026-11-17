// Логика PluginBus вырезается прямо из infinity.html, чтобы тест проверял
// РЕАЛЬНЫЙ код приложения, а не его копию. Запуск: node frontend/test_pluginbus.js
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, 'infinity.html'), 'utf8');

// Файл смешанной кодировки переносов (где-то CRLF, где-то LF), поэтому
// конец блока ищем по регулярке, а не по точному '\n};'.
function extractRe(startRe, endRe) {
  const a = src.search(startRe);
  if (a < 0) throw new Error('не найдено начало: ' + startRe);
  const rest = src.slice(a);
  const m = rest.match(endRe);
  if (!m) throw new Error('не найден конец: ' + endRe);
  return rest.slice(0, m.index + m[0].length);
}

const busSrc = extractRe(/const PluginBus=\{/, /\r?\n\};/);
const headerSrc = extractRe(/function parsePluginHeader\(/, /\r?\n  return out;\r?\n\}/);
// Окружение страницы, нужное плагинам
global.S = () => ({ plugins: { dir: 'C:/plugins', defs: [] } });
global.toast = () => {};
global.save = () => {};
global.active = () => ({ id: 's1', messages: [] });
global.uid = (p) => p + '_' + Math.random().toString(36).slice(2, 9);
global.PluginFS = { reloadOne: () => {} };

// Консоль Windows не тянет кириллицу (cp866) — пишем отчёт в UTF-8 файл
// и дублируем в stdout простым ASCII.
const lines = [];
const out = (s) => { lines.push(s); try { process.stdout.write(s + '\n'); } catch (e) {} };
const reportPath = path.join(require('os').tmpdir(), 'pluginbus-report.txt');
fs.writeFileSync(reportPath, '', 'utf8');

let pass = 0, fail = 0;
function check(label, cond, detail) {
  const mark = cond ? 'OK  ' : 'FAIL';
  if (cond) pass++; else fail++;
  const line = '[' + mark + '] ' + label + (!cond && detail ? ' — ' + detail : '');
  lines.push(line);
  try { process.stdout.write((cond ? '[OK  ] ' : '[FAIL] ') + (cond ? 'passed' : 'failed') + '\n'); } catch (e) {}
}
process.on('exit', () => {
  try { fs.appendFileSync(reportPath, lines.join('\n') + '\n', 'utf8'); } catch (e) {}
  try { process.stdout.write('report: ' + reportPath + ' | passed=' + pass + ' failed=' + fail + '\n'); } catch (e) {}
});

// В Node нет DOM и нет window, поэтому выполняем извлечённый код в этой же
// области видимости: eval делает const/function локальными для eval-области,
// поэтому явно возвращаем наружу то, что нужно тесту.
const busFactory = new Function(
  'toast','save','active','S','PluginFS',
  busSrc + '\nreturn PluginBus;'
);
const headerFactory = new Function(headerSrc + '\nreturn parsePluginHeader;');

const PluginBus = busFactory(global.toast, global.save, global.active,
                             global.S, global.PluginFS);
const parsePluginHeader = headerFactory();

const def = { id: 'p1', name: 'Тест', version: '1.0', code: `
  api.registerCommand({name:'/a',desc:'a',run:function(){return 'A'}});
  api.registerCommand({name:'/b',desc:'b',run:function(){return 'B'}});
` };
check('mount возвращает id', PluginBus.mount(def) === 'p1');
check('isOn после mount', PluginBus.isOn('p1') === true);
check('команды прочитаны', PluginBus.commands().length === 2,
  'найдено ' + PluginBus.commands().length);
check('имя команды из api', PluginBus.commands()[0].name === '/a');
check('экземпляр доступен для карточки', PluginBus.byId('p1').api._cmds.length === 2);

// ── Размонтирование снимает таймеры ────────────────────────────────────
const tdef = { id: 'p2', name: 'Таймер',
  code: `api.setInterval(function(){ api.storeSet('t', 1) }, 5);` };
PluginBus.mount(tdef);
check('таймеровый плагин смонтирован', PluginBus.isOn('p2'));
PluginBus.unmount('p2');
check('unmount снял плагин', PluginBus.isOn('p2') === false);
check('unmount убрал его команды',
  !PluginBus.commands().some(c => c.pluginId === 'p2'));
check('unmount для неизвестного id безопасен',
  PluginBus.unmount('нет-такого') === false);

// ── Повторный mount не оставляет старый экземпляр ─────────────────────
const dup = { id: 'p3', name: 'Дубль',
  code: `api.registerCommand({name:'/d',desc:'d',run:function(){return 'D'}});` };
PluginBus.mount(dup);
PluginBus.mount(dup);
check('повторный mount не дублирует экземпляр',
  PluginBus.instances.filter(i => i.id === 'p3').length === 1,
  'экземпляров: ' + PluginBus.instances.filter(i => i.id === 'p3').length);
check('повторный mount не дублирует команду',
  PluginBus.commands().filter(c => c.name === '/d').length === 1);

// ── Ошибка в коде не ломает систему ────────────────────────────────────
const bdef = { id: 'p5', name: 'Сломан', code: `throw new Error('намеренно')` };
check('mount битого плагина возвращает null', PluginBus.mount(bdef) === null);
check('битый плагин не смонтирован', PluginBus.isOn('p5') === false);
check('ошибка сохранена в def.error',
  typeof bdef.error === 'string' && bdef.error.length > 0);
check('список команд жив после ошибки', PluginBus.commands().length > 0);

// ── 'ready' для плагина, смонтированного ПОСЛЕ готовности ─────────────
PluginBus.init();                 // выставляет _ready = true
const ldef = { id: 'p6', name: 'Поздний',
  code: `api.on('ready',function(){ api.storeSet('late', 1) });` };
PluginBus.mount(ldef);
const late = PluginBus.byId('p6') && PluginBus.byId('p6').def.store.late;
check("api.on('ready') сработал для позднего плагина", late === 1, 'late=' + late);

// ── emit: хук может подменить значение ─────────────────────────────────
const sdef = { id: 'p7', name: 'Send',
  code: `api.on('send',function(t){ return t + ' [ok]' });` };
PluginBus.mount(sdef);
check('emit("send") подменяет значение',
  PluginBus.emit('send', 'привет') === 'привет [ok]');

// ── Дедупликация команд с одинаковым именем ───────────────────────────
PluginBus.mount({ id: 'p8', name: 'Первый',
  code: `api.registerCommand({name:'/dup',desc:'1',run:function(){return 1}});` });
PluginBus.mount({ id: 'p9', name: 'Второй',
  code: `api.registerCommand({name:'/dup',desc:'2',run:function(){return 2}});` });
const dups = PluginBus.commands().filter(c => c.name === '/dup');
check('дубль команды схлопнут', dups.length === 1, 'найдено ' + dups.length);
check('побеждает последний зарегистрировавший',
  dups[0] && dups[0].plugin === 'Второй', dups[0] && dups[0].plugin);

// ── Разбор заголовка файла ─────────────────────────────────────────────
const meta = parsePluginHeader(
  '// @name: Счётчик слов\n// @desc: Считает слова\n// @version: 2.1\n// @author: tester\n\napi.toast(1);',
  'counter.js');
check('заголовок: имя', meta.name === 'Счётчик слов', meta.name);
check('заголовок: описание', meta.desc === 'Считает слова', meta.desc);
check('заголовок: версия', meta.version === '2.1', meta.version);
check('заголовок: автор', meta.author === 'tester', meta.author);

const bare = parsePluginHeader("api.toast('без заголовка')", 'my-plugin.js');
check('без заголовка: имя из файла', bare.name === 'my-plugin', bare.name);
check('без заголовка: автор по умолчанию', bare.author === 'local', bare.author);
check('без заголовка: версия 1.0', bare.version === '1.0', bare.version);
check('без заголовка: описание пустое', bare.desc === '');

const late2 = parsePluginHeader('api.toast(1);\n// @name: Слишком поздно\n', 'x.js');
check('заголовок читается только из начала файла', late2.name === 'x', late2.name);

out('');
out('ИТОГ: ' + (fail === 0 ? 'все проверки пройдены' : fail + ' провалов'));
process.exit(fail === 0 ? 0 : 1);
