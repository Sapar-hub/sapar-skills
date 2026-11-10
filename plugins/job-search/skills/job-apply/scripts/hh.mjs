#!/usr/bin/env node
// hh.ru: резюме по трекам, синхронизация из tailored-yaml отклика. Работает в том же Chromium, что browser.mjs
// (профиль job-apply, пользователь залогинен как соискатель).
//
//   hh.mjs resumes                              id и названия резюме в аккаунте
//   hh.mjs map                                  трек → id из ~/.config/job-apply/hh-resumes.json
//   hh.mjs sync <track> <tailored.yaml> [--dry] переписать резюме трека: «Обо мне», ключевые навыки, ЗП пустая
//
// Что синхронизируется: per-resume поля hh — «Обо мне» (about + проекты + публикации), ключевые навыки,
// уровень дохода (всегда пусто). Название/профессия, опыт, образование, контакты — общие в профиле hh,
// их ведём руками и сверяем с master.yaml. Отклик на hh — агентом через browser.mjs (см. SKILL.md).
import { chromium } from 'playwright';
import { claimBrowser } from './lock.mjs';
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
  try { browser = await chromium.connectOverCDP(CDP); } catch { die('браузер не запущен: browser.mjs start'); }
  const ctx = browser.contexts()[0];
  const page = await ctx.newPage();                 // своя вкладка: полузаполненная форма в основной не страдает
  await page.setViewportSize({ width: 1366, height: 900 }).catch(() => {});
  return { browser, page };
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
  } finally { await page.close(); await browser.close(); }
}

const [cmd, ...args] = process.argv.slice(2);
if (cmd === 'resumes') {
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
  } finally { await page.close(); await browser.close(); }
} else {
  die('команды: resumes | map | sync <track> <tailored.yaml> [--dry]');
}
