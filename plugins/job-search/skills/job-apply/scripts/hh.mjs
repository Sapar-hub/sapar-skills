#!/usr/bin/env node
// hh.ru: резюме по трекам, синхронизация из tailored-yaml отклика. Работает в том же Chromium, что browser.mjs
// (профиль job-apply, пользователь залогинен как соискатель).
//
//   hh.mjs resumes                              id и названия резюме в аккаунте
//   hh.mjs map                                  трек → id из ~/.config/job-apply/hh-resumes.json
//   hh.mjs sync <track> <tailored.yaml> [--dry] переписать резюме трека: «Обо мне», ключевые навыки, ЗП пустая
//   hh.mjs find --title T --company C          та же вакансия на hh: строки FOUND|APPLIED <url> <должность> <работодатель>
//                                                <оценка> (до 3, лучшая первой) или NONE
//   hh.mjs search --text Q [--days 1] [--area 1] [--exp noExperience]   свежие вакансии из поиска hh, JSON:
//                                                [{id, url, title, employer, exp, address, applied}]
//   hh.mjs negotiations [--json]               мои отклики на hh со статусом: TSV <статус> <дата> <url> <должность>
//                                                <работодатель>; статус = invitation|rejection|viewed|not-viewed|<тег hh>
//   hh.mjs chat <id вакансии> [--send-file F]   чат с работодателем по отклику, JSON {found, open, remind, tail, chat,
//                                                sent}: open — есть поле ввода (работодатель не закрыл переписку);
//                                                --send-file — отправить текст из файла (только если open)
//
// Что синхронизируется: per-resume поля hh — «Обо мне» (about + проекты + публикации), ключевые навыки,
// уровень дохода (всегда пусто). Название/профессия, опыт, образование, контакты — общие в профиле hh,
// их ведём руками и сверяем с master.yaml. Отклик на hh — агентом через browser.mjs (см. SKILL.md).
import { chromium } from 'playwright';
import { claimBrowser } from './lock.mjs';
import { closeHungTabs } from './cdp-health.mjs';
import { readFileSync, existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import YAML from 'yaml';

const HOME = process.env.JOB_APPLY_HOME || join(homedir(), '.local/share/job-apply');
const MAP_FILE = process.env.JOB_APPLY_HH_MAP || join(homedir(), '.config/job-apply/hh-resumes.json');
const CDP = `http://127.0.0.1:${Number(process.env.JOB_APPLY_PORT || 9333)}`;
const ABOUT_MAX = 3500;            // «Обо мне» на hh: держим компактно, длинное всё равно не читают
const SKILLS_MAX = 30;

const die = (m) => { console.error(`ERR: ${m}`); process.exit(1); };
const say = (m) => console.log(m);

function loadMap() {
  if (!existsSync(MAP_FILE)) die(`нет ${MAP_FILE}: {"tracks": {"ml": "<id>"}, "aliases": {"DS": "ml"}}`);
  return JSON.parse(readFileSync(MAP_FILE, 'utf8'));
}

function resolveTrack(map, track) {
  const t = map.aliases?.[track] || map.aliases?.[track.toUpperCase()] || track;
  const id = map.tracks?.[t];
  if (!id) die(`трек «${track}» не привязан к резюме hh; есть: ${Object.keys(map.tracks || {}).join(', ')}`);
  return { track: t, id };
}

async function connect() {
  await claimBrowser(HOME);                         // headless-прогоны: браузер один, ждём своей очереди (lock.mjs)
  let browser;
  await closeHungTabs(Number(process.env.JOB_APPLY_PORT || 9333));   // зависшая вкладка вешает connectOverCDP
  try { browser = await chromium.connectOverCDP(CDP); } catch { die('браузер не запущен: browser.mjs start'); }
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();                 // своя вкладка: полузаполненная форма в основной не страдает
  await page.setViewportSize({ width: 1366, height: 900 }).catch(() => {});
  // id вкладки — чтобы закрыть её через HTTP CDP, если сама вкладка зависла (page.close тогда тоже висит)
  try {
    const s = await ctx.newCDPSession(page);
    TAB_ID = (await s.send('Target.getTargetInfo')).targetInfo.targetId;
    await s.detach().catch(() => {});
  } catch {}
  // сторож: команда не уложилась в HH_TIMEOUT (с) — закрыть свою вкладку и выйти, а не оставлять висящий таб
  setTimeout(async () => {
    console.error(`ERR: hh.mjs не уложился в ${WATCHDOG_S} с — закрываю свою вкладку`);
    await dropTab();
    process.exit(124);
  }, WATCHDOG_S * 1000).unref();
  return { browser, page };
}

let TAB_ID = '';
const WATCHDOG_S = Number(process.env.HH_TIMEOUT || 300);
async function dropTab() {
  if (!TAB_ID) return;
  await fetch(`${CDP}/json/close/${TAB_ID}`, { signal: AbortSignal.timeout(5000) }).catch(() => {});
}
// закрыть свою вкладку и отключиться; зависшую вкладку — через HTTP CDP
async function done(page, browser) {
  const ok = await Promise.race([page.close().then(() => true, () => false),
    new Promise((r) => setTimeout(() => r(false), 8000))]);
  if (!ok) await dropTab();
  await Promise.race([browser.close().catch(() => {}), new Promise((r) => setTimeout(r, 5000))]);
}

async function open(page, url) {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForLoadState('load', { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(1500);
  if (/account\/login/.test(page.url())) die('сессия hh слетела: залогинься в окне браузера (hh.ru/account/login)');
  if (/^404/.test(await page.title())) die(`404: ${url}`);
}

// --- текст «Обо мне» из tailored-yaml ---
const clean = (s) => String(s ?? '').replace(/\s+/g, ' ').trim();

export function aboutText(r) {
  const parts = [];
  if (r.headline) parts.push(clean(r.headline));
  if (r.about) parts.push(clean(r.about));
  const projects = (r.projects || []).map((p) => {
    const head = `— ${clean(p.title)}${p.period ? ` (${p.period})` : ''}`;
    const lines = (p.bullets || []).slice(0, 2).map((b) => `  ${clean(b)}`);
    if (p.metrics) lines.push(`  ${clean(p.metrics)}`);
    if (p.links?.length) lines.push(`  ${p.links.map(clean).join(' · ')}`);
    return [head, ...lines].join('\n');
  });
  if (projects.length) parts.push(`Проекты:\n${projects.join('\n')}`);
  if (r.publications?.length) parts.push(`Публикации:\n${r.publications.map((p) => `— ${clean(p)}`).join('\n')}`);
  if (r.contacts?.github) parts.push(`GitHub: ${r.contacts.github}`);
  let text = parts.join('\n\n');
  // режем по проектам с конца, пока не влезет
  while (text.length > ABOUT_MAX && projects.length > 1) {
    projects.pop();
    parts.splice(parts.findIndex((x) => x.startsWith('Проекты:')), 1, `Проекты:\n${projects.join('\n')}`);
    text = parts.join('\n\n');
  }
  return text.slice(0, ABOUT_MAX);
}

const SKILL_NOISE = new Set(['интервалы', 'нормализация', 'scd2', 'bootstrap', 'доверительные интервалы']);

export function skillList(r) {
  // сначала формулировки вакансии (ats_keywords), потом группы навыков резюме
  const out = [];
  const seen = new Set();
  const push = (raw) => {
    const s = clean(String(raw).replace(/\s*\([^)]*\)\s*$/, ''));   // «Python (продвинутый)» → «Python»
    const k = s.toLowerCase();
    if (!s || s.length > 40 || /изучаю|;/.test(s) || SKILL_NOISE.has(k) || seen.has(k)) return;
    seen.add(k); out.push(s);
  };
  (r.ats_keywords || []).forEach(push);
  for (const group of Object.values(r.skills || {})) (group || []).forEach(push);
  return out.slice(0, SKILLS_MAX);
}

// --- секции резюме ---
async function clickSave(page) {
  const btn = page.getByRole('button', { name: /^(Save|Сохранить)$/ }).last();
  await btn.click();
  const done = () => !/\/resume\/edit\//.test(page.url()) || /skillsLevels/.test(page.url());  // после навыков hh ведёт на уровни
  for (let i = 0; i < 15 && !done(); i++) await page.waitForTimeout(1000);
  await page.waitForTimeout(800);
  if (/Кем вы хотите работать|What do you want to do/.test(await page.locator('body').innerText().catch(() => ''))) {
    die('hh просит профессию: резюме-черновик без «Профессии» — заполни /position руками один раз');
  }
  if (!done()) {
    const err = await page.locator('[data-qa*="error"], [class*="error"]').allInnerTexts().catch(() => []);
    die(`не сохранилось (${page.url()}): ${err.filter(Boolean).join(' | ').slice(0, 300) || 'см. окно браузера'}`);
  }
}

async function syncSalary(page, id) {
  await open(page, `https://hh.ru/resume/edit/${id}/position`);
  const f = page.getByLabel(/Уровень дохода|Salary|income/i).first();
  const v = await f.inputValue().catch(() => '');
  if (!v || v === '0') { say('  ЗП: уже пусто'); return; }
  await f.fill('');
  await clickSave(page);
  say(`  ЗП: было ${v} → пусто`);
}

async function syncAbout(page, id, text) {
  await open(page, `https://hh.ru/resume/edit/${id}/about`);
  const ta = page.locator('textarea').first();
  const old = await ta.inputValue();
  if (old.trim() === text.trim()) { say('  «Обо мне»: без изменений'); return; }
  await ta.fill(text);
  await clickSave(page);
  say(`  «Обо мне»: ${old.length} → ${text.length} симв.`);
}

async function syncSkills(page, id, skills) {
  await open(page, `https://hh.ru/resume/edit/${id}/keySkills`);
  // снять все текущие
  for (let i = 0; i < 80; i++) {
    const del = page.getByRole('button', { name: /^(Delete|Удалить)$/ });
    if (!(await del.count())) break;
    await del.first().click();
    await page.waitForTimeout(150);
  }
  const box = page.getByRole('combobox').first();
  for (const s of skills) {
    await box.click();
    await box.fill(s);
    await page.waitForTimeout(250);
    await box.press('Enter');
    await page.waitForTimeout(250);
  }
  const added = await page.getByRole('button', { name: /^(Delete|Удалить)$/ }).count();
  if (added < Math.min(skills.length, SKILLS_MAX) * 0.8) {
    die(`добавилось ${added} навыков из ${skills.length} — проверь окно, ничего не сохранено`);
  }
  await clickSave(page);
  say(`  навыки: ${added} шт.`);
}

async function listResumes() {
  const { browser, page } = await connect();
  try {
    await open(page, 'https://hh.ru/applicant/resumes');
    const rows = await page.evaluate(() => {
      const seen = new Map();
      for (const a of document.querySelectorAll('a[href*="/resume/"], [href*="/profile/resume?resume="]')) {
        const m = /(?:\/resume\/|resume=)([0-9a-f]{30,})/.exec(a.getAttribute('href') || '');
        if (!m || seen.has(m[1])) continue;
        seen.set(m[1], (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 90));
      }
      return [...seen.entries()];
    });
    for (const [id, t] of rows) say(`${id}\t${t || '(без названия / черновик)'}`);
  } finally { await done(page, browser); }
}

// --- поиск вакансии на hh по должности и компании (второй канал к отклику на сайте работодателя) ---
const norm = (s) => clean(s).toLowerCase().replace(/[«»"'().,–—-]/g, ' ').replace(/\b(ооо|пао|ао|зао|llc|ltd|group|tech)\b/g, ' ')
  .replace(/\s+/g, ' ').trim();

async function find(title, company) {
  const { browser, page } = await connect();
  try {
    // должность без компании: поиск hh склеивает слова через И, компания только сужает; работодателя сверяем сами
    const q = encodeURIComponent(clean(title).replace(/[\[\](){}«»"]/g, ' ').slice(0, 120));
    await open(page, `https://hh.ru/search/vacancy?text=${q}&area=113&order_by=relevance&items_on_page=50`);
    const cards = await page.evaluate(() => [...document.querySelectorAll('a[data-qa="serp-item__title"], a[data-qa*="serp-item__title"]')]
      .filter((a) => /\/vacancy\/\d+/.test(a.href))
      .map((a) => {
        let card = a; for (let i = 0; i < 10 && card.parentElement; i++) {
          card = card.parentElement; if (card.querySelector('[data-qa="vacancy-serp__vacancy-employer-text"]')) break; }
        const t = (sel) => (card.querySelector(sel)?.textContent || '').trim();
        return {
          id: a.href.match(/\/vacancy\/(\d+)/)[1], title: (a.textContent || '').trim(),
          employer: t('[data-qa="vacancy-serp__vacancy-employer-text"]') || t('[data-qa="vacancy-serp__vacancy-employer"]'),
          responded: /Вы откликнулись|You applied|You have applied|Отклик отправлен/i.test(card.textContent || ''),
        };
      }));
    const seen = new Set(), nc = norm(company), firstWord = nc.split(' ')[0] || '';
    // общие слова должности не в счёт: «Стажер (помощник HR)» ≠ «Стажер LLM-researcher»
    const GENERIC = new Set(['стажер', 'стажёр', 'стажировка', 'junior', 'младший', 'начинающий', 'intern', 'internship',
      'специалист', 'senior', 'middle', 'ведущий', 'старший', 'команда', 'команду', 'отдел', 'управление', 'группа']);
    const words = norm(title).split(' ').filter((w) => w.length > 2 && !GENERIC.has(w));
    // уровень должен совпадать: «Стажер LLM-researcher» ≠ «Middle LLM Researcher»
    const level = (t) => (/стаж|intern|junior|джуниор|младш|начинающ/i.test(t) ? 'junior'
      : /middle|мидл|senior|сеньор|ведущ|старш|lead|лид|главн|руковод|head/i.test(t) ? 'senior' : '');
    const want = level(title);
    const scored = cards.filter((c) => !seen.has(c.id) && seen.add(c.id))
      .filter((c) => !want || !level(c.title) || level(c.title) === want).map((c) => {
      const ne = norm(c.employer), nt = norm(c.title);
      const emp = nc && (ne.includes(nc) || nc.includes(ne) || (firstWord.length > 2 && ne.includes(firstWord)));
      const hit = words.filter((w) => nt.includes(w)).length / Math.max(words.length, 1);
      return { ...c, score: (emp ? 1 : 0) + hit };
    }).filter((c) => c.score >= 1.8).sort((a, b) => b.score - a.score);   // работодатель + ≥80% слов должности
    if (!scored.length) { say('NONE'); return; }
    for (const c of scored.slice(0, 3))
      say(`${c.responded ? 'APPLIED' : 'FOUND'}\thttps://hh.ru/vacancy/${c.id}\t${c.title}\t${c.employer}\t${c.score.toFixed(2)}`);
  } finally { await done(page, browser); }
}

// --- поиск: источник для дайджеста (job-digest/scripts/digest.py, ~/.config/job-digest/hh.txt) ---
async function search({ text, days = '1', area = '1', exp = 'noExperience' }) {
  const { browser, page } = await connect();
  const out = [], seen = new Set();
  try {
    for (let n = 0; n < 5; n++) {
      const q = new URLSearchParams({ text, area, search_period: days, order_by: 'publication_time',
        items_on_page: '50', page: String(n) });
      if (exp) q.set('experience', exp);
      await open(page, `https://hh.ru/search/vacancy?${q}`);
      const cards = await page.evaluate(() => [...document.querySelectorAll('[data-qa^="vacancy-serp__vacancy"][data-qa$="vacancy"], [data-qa="vacancy-serp__vacancy"]')]
        .map((card) => {
          const a = card.querySelector('a[data-qa="serp-item__title"], a[data-qa*="serp-item__title"]');
          const t = (sel) => (card.querySelector(sel)?.textContent || '').replace(/\s+/g, ' ').trim();
          const id = (a?.href.match(/\/vacancy\/(\d+)/) || [])[1];
          return id && {
            id, title: (a.textContent || '').trim(),
            employer: t('[data-qa="vacancy-serp__vacancy-employer-text"]') || t('[data-qa="vacancy-serp__vacancy-employer"]'),
            exp: t('[data-qa^="vacancy-serp__vacancy-work-experience"]'),
            address: t('[data-qa="vacancy-serp__vacancy-address"]'),
            applied: /Вы откликнулись|You applied|You have applied|Отклик отправлен/i.test(card.textContent || ''),
          };
        }).filter(Boolean));
      for (const c of cards) if (!seen.has(c.id)) { seen.add(c.id); out.push({ ...c, url: `https://hh.ru/vacancy/${c.id}` }); }
      if (cards.length < 50) break;
    }
  } finally { await done(page, browser); }
  say(JSON.stringify(out));
}

// --- мои отклики (applicant/negotiations): статус работодателя по каждому ---
async function negotiations(asJson) {
  const { browser, page } = await connect();
  const out = [];
  try {
    for (let n = 0; n < 20; n++) {
      await open(page, `https://hh.ru/applicant/negotiations?filter=all&page=${n}`);
      const items = await page.evaluate(() => [...document.querySelectorAll('[data-qa="negotiations-item"]')].map((it) => {
        const a = it.querySelector('[data-qa="negotiations-item-vacancy"] a[href*="/vacancy/"], a[href*="/vacancy/"]');
        const tag = it.querySelector('[data-qa^="negotiations-tag"]');
        const qa = (tag?.getAttribute('data-qa') || '').match(/negotiations-item-([\w-]+)/);
        const t = (sel) => (it.querySelector(sel)?.textContent || '').trim();
        return {
          id: (a?.href.match(/\/vacancy\/(\d+)/) || [])[1] || '',
          title: (a?.textContent || t('[data-qa="negotiations-item-vacancy"]')).trim(),
          company: t('[data-qa="negotiations-item-company"]'), date: t('[data-qa="negotiations-item-date"]'),
          tag: qa ? qa[1] : '', tagText: (tag?.textContent || '').trim(),
        };
      }));
      if (!items.length) break;
      out.push(...items);
      const more = await page.locator('[data-qa="pager-next"]').count();
      if (!more) break;
    }
  } finally { await done(page, browser); }
  // «discard» = отказ; приглашение/собеседование/выход — всё, что зовёт дальше
  const status = (x) => x.tag === 'discard' ? 'rejection'
    : /invit|interview|hired|offer|phone/.test(x.tag) || /Приглаш|Invitation|Interview|Собесед|Выход|Work start/i.test(x.tagText) ? 'invitation'
    : x.tag || (/Отказ|Rejection/i.test(x.tagText) ? 'rejection' : 'unknown');
  const rows = out.map((x) => ({ status: status(x), date: x.date, url: x.id ? `https://hh.ru/vacancy/${x.id}` : '',
    title: x.title, company: x.company, tag: x.tagText }));
  if (asJson) say(JSON.stringify(rows, null, 1));
  else for (const r of rows) say([r.status, r.date, r.url, r.title, r.company].map((v) => clean(v).replace(/\t/g, ' ')).join('\t'));
}

// --- чат по отклику: открыт ли (есть поле ввода), хвост переписки, отправка ---
async function chat(vid, sendText) {
  const { browser, page } = await connect();
  const res = { vacancy: vid, found: false };
  try {
    for (let n = 0; n < 20; n++) {
      await open(page, `https://hh.ru/applicant/negotiations?filter=all&page=${n}`);
      const item = page.locator('[data-qa="negotiations-item"]')
        .filter({ has: page.locator(`a[href*="/vacancy/${vid}"]`) }).first();
      if (await item.count()) {
        res.found = true;
        await item.getByRole('button', { name: /chat|чат/i }).first().click();
        break;
      }
      if (!(await page.locator('[data-qa="pager-next"]').count())) break;
    }
    if (!res.found) return;
    let fr = null;
    for (let i = 0; i < 60 && !fr; i++) {
      await page.waitForTimeout(250);
      fr = page.frames().find((f) => /chatik\.hh\.ru\/chat\/\d+/.test(f.url()));
    }
    if (!fr) die('чат не открылся');
    await page.waitForTimeout(2000);                         // переписка дорисовывается после загрузки фрейма
    res.chat = fr.url().split('?')[0];
    const ta = fr.locator('textarea');
    res.open = (await ta.count()) > 0 && await ta.first().isEnabled().catch(() => false);
    res.remind = (await fr.getByRole('button', { name: /Напомнить об отклике/ }).count()) > 0;
    const body = await fr.locator('body').innerText().catch(() => '');
    res.tail = clean(body).slice(-600);
    if (sendText) {
      if (!res.open) die('чат закрыт работодателем — писать нельзя');
      await ta.first().fill(sendText);
      await ta.first().press('Enter');                       // Enter — отправить, Shift+Enter — новая строка
      await page.waitForTimeout(2500);
      const after = clean(await fr.locator('body').innerText().catch(() => ''));
      res.sent = after.includes(clean(sendText).slice(0, 40)) && (await ta.first().inputValue().catch(() => 'x')) === '';
    }
  } finally {
    await done(page, browser);
    say(JSON.stringify(res));
  }
}

const [cmd, ...args] = process.argv.slice(2);
if (cmd === 'chat') {
  const vid = (args[0] || '').match(/\d+/)?.[0];
  if (!vid) die('hh.mjs chat <id вакансии> [--send-file F]');
  const i = args.indexOf('--send-file');
  await chat(vid, i >= 0 ? readFileSync(args[i + 1], 'utf8').trim() : '');
} else if (cmd === 'resumes') {
  await listResumes();
} else if (cmd === 'map') {
  const m = loadMap();
  for (const [t, id] of Object.entries(m.tracks || {})) say(`${t}\t${id}\thttps://hh.ru/resume/${id}`);
  for (const [a, t] of Object.entries(m.aliases || {})) say(`  ${a} → ${t}`);
} else if (cmd === 'sync') {
  const [track, file] = args.filter((a) => !a.startsWith('--'));
  if (!track || !file) die('hh.mjs sync <track> <tailored.yaml> [--dry]');
  const r = YAML.parse(readFileSync(file, 'utf8'));
  const { track: t, id } = resolveTrack(loadMap(), track);
  const text = aboutText(r), skills = skillList(r);
  if (args.includes('--dry')) {
    say(`# ${t} → https://hh.ru/resume/${id}\n## Обо мне (${text.length} симв.)\n${text}\n\n## Навыки (${skills.length})\n${skills.join(', ')}`);
    process.exit(0);
  }
  mkdirSync(join(HOME, 'hh-backup'), { recursive: true });
  const { browser, page } = await connect();
  try {
    // резервная копия «Обо мне» перед перезаписью
    await open(page, `https://hh.ru/resume/edit/${id}/about`);
    const old = await page.locator('textarea').first().inputValue().catch(() => '');
    await open(page, `https://hh.ru/resume/${id}`);
    const oldSkills = await page.evaluate(() => {
      const h = [...document.querySelectorAll('h2,h3,[data-qa*="title"]')].find((e) => /Навыки|Skills/.test(e.innerText));
      return h ? h.closest('section, div')?.parentElement?.innerText || '' : '';
    }).catch(() => '');
    writeFileSync(join(HOME, 'hh-backup', `${new Date().toISOString().slice(0, 10)}_${t}_${id}.txt`),
      `## Обо мне\n${old}\n\n## Навыки (как на странице)\n${oldSkills}\n`);
    say(`sync ${t} → https://hh.ru/resume/${id}`);
    await syncSalary(page, id);
    await syncAbout(page, id, text);
    await syncSkills(page, id, skills);
    say('OK');
  } finally { await done(page, browser); }
} else if (cmd === 'find') {
  const opt = (k) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : ''; };
  if (!opt('--title') || !opt('--company')) die('hh.mjs find --title <должность> --company <компания>');
  await find(opt('--title'), opt('--company'));
} else if (cmd === 'search') {
  const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
  if (!opt('--text')) die('hh.mjs search --text <запрос> [--days 1] [--area 1] [--exp noExperience|between1And3|""]');
  await search({ text: opt('--text'), days: opt('--days', '1'), area: opt('--area', '1'), exp: opt('--exp', 'noExperience') });
} else if (cmd === 'negotiations') {
  await negotiations(args.includes('--json'));
} else {
  die('команды: resumes | map | sync <track> <tailored.yaml> [--dry] | find --title T --company C | search --text Q | negotiations [--json]');
}
