// Браузер один (профиль с логинами), headless-прогонов несколько: кто первым позвал browser.mjs/hh.mjs/relay.mjs,
// тот и держит браузер до конца своего прогона. Остальные ждут. Задачам без браузера лок не нужен вовсе.
//
// Прогон = JOB_APPLY_RUN=<id> в окружении + файл $JOB_APPLY_HOME/runs/<id> с pid процесса, который живёт весь прогон
// (раннер задач tg-bridge или worker.sh). Его ставит и убирает запускающий; владелец браузера — $JOB_APPLY_HOME/browser.owner.
// Нет JOB_APPLY_RUN (ручной запуск у ноута) — лок не берём и не ждём.
import { readFileSync, writeFileSync, existsSync, mkdirSync, openSync, closeSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';

const WAIT_MIN = Number(process.env.JOB_APPLY_BROWSER_WAIT_MIN || 30);

function alive(home, run) {
  const f = join(home, 'runs', run);
  if (!existsSync(f)) return false;
  const pid = Number(readFileSync(f, 'utf8').split('\n')[0]);
  try { process.kill(pid, 0); return true; } catch { return false; }
}

function title(home, run) {
  try { return readFileSync(join(home, 'runs', run), 'utf8').split('\n')[1] || run; } catch { return run; }
}

export async function claimBrowser(home) {
  const me = process.env.JOB_APPLY_RUN;
  if (!me) return;
  mkdirSync(home, { recursive: true });
  const owner = join(home, 'browser.owner');
  const deadline = Date.now() + WAIT_MIN * 60_000;
  let said = false;
  for (;;) {
    try {                                                     // 'wx': создать, только если нет — атомарно
      const fd = openSync(owner, 'wx');
      writeFileSync(fd, me);
      closeSync(fd);
      return;
    } catch {}
    let cur = '';
    try { cur = readFileSync(owner, 'utf8').trim(); } catch { continue; }
    if (cur === me) return;
    if (!cur || !alive(home, cur)) {                          // владелец умер, не отпустив, — снимаем
      try { unlinkSync(owner); } catch {}
      continue;
    }
    if (Date.now() > deadline) {
      console.error(`ERR: браузер занят другим прогоном («${title(home, cur)}») дольше ${WAIT_MIN} мин`);
      process.exit(3);
    }
    if (!said) { console.error(`браузер занят: «${title(home, cur)}», жду…`); said = true; }
    await new Promise((r) => setTimeout(r, 3000));
  }
}

// --- гейт отправки -------------------------------------------------------------------------------------------
// В headless-прогоне (JOB_APPLY_RUN) нажать «Откликнуться/Отправить» можно только с одноразовым токеном, который
// выдаёт gate.py после кнопки «отправить» в Telegram (или по no_confirm из auto.yaml). Модель обойти это не может:
// без файла-токена browser.mjs откажет, сколько бы она ни просила.
export const SUBMIT_RE = /(откликн|отправ|подать|подтверд.*отклик|завершить|submit|apply|send\b)/i;

export function needToken() { return !!process.env.JOB_APPLY_RUN; }

export function useToken(home, given) {
  const run = process.env.JOB_APPLY_RUN;
  const f = join(home, 'gate', `${run}.token`);
  let want = '';
  try { want = readFileSync(f, 'utf8').trim(); } catch {}
  if (!want || !given || given !== want) return false;
  try { unlinkSync(f); } catch {}                              // одноразовый
  return true;
}

// элемент — отправка формы отклика: type=submit, или кнопка с текстом отправки ВНУТРИ контейнера формы отклика
// (form или ближайший предок с полем файла/почты/телефона). «Откликнуться» на странице вакансии, которая только
// открывает форму, и «Загрузить резюме» под это не попадают.
export const IS_SUBMIT_JS = (el) => {
  const t = ((el.innerText || el.value || el.getAttribute('aria-label') || '') + '').trim();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (type === 'submit') return true;
  const service = /загруз|прикреп|upload|attach|выбр|добав|удал|показ|скры|ещё|еще|add|remove|show|hide/i.test(t);
  // <button> без type внутри <form> по стандарту HTML отправляет форму, какой бы ни был текст
  if (el.tagName === 'BUTTON' && !type && el.closest('form') && !service) return true;
  if (!/(откликн|отправ|подать|завершить|submit|apply|send)/i.test(t) || service) return false;
  let box = el.closest('form');
  for (let p = el.parentElement, i = 0; !box && p && i < 8; p = p.parentElement, i++)
    if (p.querySelector('input[type=file], input[type=email], input[type=tel]')) box = p;
  return !!box;
};

// Enter внутри формы отклика (есть файл/почта/телефон) = отправка
export const ENTER_SUBMITS_JS = () => {
  const f = document.activeElement && document.activeElement.closest && document.activeElement.closest('form');
  return !!(f && f.querySelector('input[type=file], input[type=email], input[type=tel]'));
};
