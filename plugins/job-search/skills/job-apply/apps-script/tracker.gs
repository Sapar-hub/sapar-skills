/**
 * job_tracker web app: headless-агенты (claude -p, задачи из Telegram) пишут в лист без OAuth.
 *
 * Установка (один раз, в самой таблице job_tracker):
 *   1. Расширения → Apps Script → вставить этот файл целиком вместо Code.gs, сохранить.
 *   2. Настройки проекта (шестерёнка) → Свойства скрипта → добавить TRACKER_SECRET = длинная случайная строка
 *      (сгенерировать: `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`).
 *   3. Начать развёртывание → Новое развёртывание → тип «Веб-приложение»:
 *      «Выполнять как: я», «Доступ: все» → Развернуть → разрешить доступ к таблицам → скопировать URL (…/exec).
 *   4. На ноуте: ~/.config/job-apply/sheet.env (chmod 600):
 *        TRACKER_URL=https://script.google.com/macros/s/…/exec
 *        TRACKER_SECRET=<то же значение>
 *      Проверка: python3 -I scripts/sheet.py ping
 *
 * Доступ «все» = URL без секрета ничего не отдаёт и не пишет (doGet — только «живой»).
 * После правки кода: Развёртывания → Управление → карандаш → Версия: новая (URL тот же).
 */
const SHEET = 'Applications';
const COLS = 'ABCDEFGHIJKLMN';

function doPost(e) {
  let req;
  try { req = JSON.parse((e && e.postData && e.postData.contents) || '{}'); }
  catch (err) { return out({ ok: false, error: 'bad json' }); }
  const secret = PropertiesService.getScriptProperties().getProperty('TRACKER_SECRET');
  if (!secret || req.secret !== secret) return out({ ok: false, error: 'forbidden' });
  const sh = SpreadsheetApp.getActive().getSheetByName(SHEET);
  if (!sh) return out({ ok: false, error: 'no sheet ' + SHEET });
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    switch (req.op) {
      case 'ping':   return out({ ok: true, last_row: lastRow(sh) });
      case 'find':   return out({ ok: true, rows: find(sh, String(req.query || ''), Number(req.limit) || 20) });
      case 'append': return out(append(sh, req.row, !!req.force));
      case 'update': return out(update(sh, req.row_number, req.set || {}, req.append || {}));
      default:       return out({ ok: false, error: 'unknown op' });
    }
  } catch (err) {
    return out({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

function doGet() { return out({ ok: true }); }

function out(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

// последняя непустая строка по столбцу A (в листе есть пустые строки-разделители — append «в дыру» не годится)
function lastRow(sh) {
  const v = sh.getRange(1, 1, sh.getMaxRows(), 1).getDisplayValues();
  for (let i = v.length - 1; i >= 0; i--) if (String(v[i][0]).trim()) return i + 1;
  return 1;
}

// текст, который Sheets принял бы за формулу или число со знаком («+7 925…», «=…»), — как текст
function cell(v) {
  const s = String(v == null ? '' : v);
  return /^[=+\-@]/.test(s) ? "'" + s : s;
}

function find(sh, q, limit) {
  const n = lastRow(sh);
  if (n < 2) return [];
  const vals = sh.getRange(2, 1, n - 1, 14).getDisplayValues();
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const res = [];
  vals.forEach((r, i) => {
    if (!String(r[0]).trim()) return;
    const hay = (r[0] + ' ' + r[1] + ' ' + r[8]).toLowerCase();
    if (words.every((w) => hay.indexOf(w) >= 0)) res.push({ row: i + 2, values: r });
  });
  return res.slice(-limit);
}

function append(sh, row, force) {
  if (!Array.isArray(row) || row.length !== 14) return { ok: false, error: 'need 14 values A..N' };
  if (!row[0] || !row[1] || !row[2]) return { ok: false, error: 'A, B, C required' };
  const n = lastRow(sh);
  if (!force && n > 1) {
    const vals = sh.getRange(2, 1, n - 1, 3).getDisplayValues();
    for (let i = 0; i < vals.length; i++) {
      if (vals[i][0] === String(row[0]) && vals[i][1] === String(row[1]) && vals[i][2] === String(row[2]))
        return { ok: true, duplicate: true, row: i + 2 };
    }
  }
  sh.getRange(n + 1, 1, 1, 14).setValues([row.map(cell)]);
  return { ok: true, row: n + 1 };
}

function update(sh, rn, set, app) {
  rn = Number(rn);
  if (!(rn > 1) || rn > lastRow(sh)) return { ok: false, error: 'bad row_number' };
  for (const [c, v] of Object.entries(set)) {
    const i = COLS.indexOf(c);
    if (i < 0) return { ok: false, error: 'bad column ' + c };
    sh.getRange(rn, i + 1).setValue(cell(v));
  }
  for (const [c, v] of Object.entries(app)) {                // дописать строкой в конец ячейки (переписка в N)
    const i = COLS.indexOf(c);
    if (i < 0) return { ok: false, error: 'bad column ' + c };
    const r = sh.getRange(rn, i + 1);
    const cur = String(r.getDisplayValue());
    r.setValue(cell(cur ? cur + '\n' + v : v));
  }
  return { ok: true, row: rn, values: sh.getRange(rn, 1, 1, 14).getDisplayValues()[0] };
}
