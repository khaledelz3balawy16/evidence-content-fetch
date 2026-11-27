/* Read-only Aptem collector. Writes local captures only; never imports DB settings.
 * All allowed POSTs are login or paginated read endpoints. No scheduler is started.
 */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {chromium} = require('../.playwright-runtime/playwright/driver/package');
const ORIGIN = 'https://kentbusinesscollege.aptem.co.uk';
const ENDPOINTS = {
  episodes: '/MWS.Review/Review/ReadReviewsEpisodes',
  groups: '/MWS.eDocuments/DynamicDocuments/ReadReviewDocumentsGroups',
  documents: '/MWS.eDocuments/DynamicDocuments/ReadComplianceDocuments',
  signatures: '/MWS.eDocuments/Signatures/SignaturesRead',
  modern: '/api/MWS.Review/ReviewGrid/Get',
};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const hash = data => crypto.createHash('sha256').update(data).digest('hex');
function args(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i += 2) {
    if (!argv[i].startsWith('--') || !argv[i + 1]) throw Error('Expected --option value');
    result[argv[i].slice(2)] = argv[i + 1];
  }
  for (const key of ['credentials', 'candidates', 'output']) if (!result[key]) throw Error('Missing --' + key);
  result.workers = Number(result.workers || 3);
  if (![1, 2, 3, 4].includes(result.workers)) throw Error('Use 1 to 4 workers');
  return result;
}
function atomicJson(file, data) {
  const body = JSON.stringify(data, null, 2);
  // Windows editors/AV can briefly deny replacement while reading progress.
  // Retry the SAME publication, without treating a sharing violation as an
  // Aptem error or closing the browser underneath the other workers.
  const retry = operation => {
    for (let attempt = 0; ; attempt++) {
      try { return operation(); } catch (error) {
        if (!['EPERM', 'EACCES', 'EBUSY'].includes(error.code) || attempt >= 14) throw error;
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, Math.min(500, 30 * (attempt + 1)));
      }
    }
  };
  retry(() => fs.writeFileSync(file + '.tmp', body));
  retry(() => fs.renameSync(file + '.tmp', file));
  retry(() => fs.writeFileSync(file + '.sha256', hash(body)));
}
function readCredentials(file) {
  const env = {};
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^(APTEM_LOGIN_(?:EMAIL|PASSWORD))\s*=\s*(.*)$/);
    if (m) env[m[1]] = m[2].trim().replace(/^(['"])(.*)\1$/, '$2');
  }
  if (!env.APTEM_LOGIN_EMAIL || !env.APTEM_LOGIN_PASSWORD) throw Error('Aptem credentials are unavailable');
  return env;
}
function emptyErrors(value) {
  return value == null || value === '' || (typeof value === 'object' && Object.keys(value).length === 0);
}
function reviewScope(bucket) {
  // Preserve the original pagination proof and account for excluded Enrollment.
  let offset = 0;
  const keep = row => !/enrol/i.test(String(row.TypeName || ''));
  const pages = bucket.pages.map(page => {
    const rows = bucket.items.slice(offset, offset + page.count);
    offset += page.count;
    return {...page, source_count: page.count, count: rows.filter(keep).length};
  });
  const items = bucket.items.filter(keep);
  return {...bucket, total: items.length, items, pages,
    source_total: bucket.total, excluded_enrollment_ids: bucket.items.filter(r => !keep(r)).map(r => String(r.Id))};
}
async function main() {
  const options = args(process.argv.slice(2));
  const credentials = readCredentials(options.credentials);
  const candidatesBody = fs.readFileSync(options.candidates, 'utf8').replace(/^\uFEFF/, '');
  const candidates = JSON.parse(candidatesBody);
  if (!Array.isArray(candidates) || candidates.some(c => !/^[1-9][0-9]*$/.test(String(c.aptem_id)))
      || new Set(candidates.map(c => String(c.aptem_id))).size !== candidates.length)
    throw Error('Candidate learner identities are invalid or duplicated');
  const selected = options.learners ? new Set(options.learners.split(',')) : null;
  if (selected && [...selected].some(id => !candidates.some(c => String(c.aptem_id) === id)))
    throw Error('Selected learner is absent from canonical inventory');
  const out = path.resolve(options.output);
  fs.mkdirSync(path.join(out, 'learners'), {recursive: true});
  const manifestPath = path.join(out, 'manifest.json');
  let manifest;
  if (fs.existsSync(manifestPath)) {
    manifest = JSON.parse(fs.readFileSync(manifestPath));
    if (manifest.candidates_sha256 !== hash(candidatesBody)) throw Error('Candidate inventory differs from this capture run');
    if (Date.now() - Date.parse(manifest.started_at) > 24 * 3600 * 1000) throw Error('Use a new output directory for a fresh capture run');
  } else {
    manifest = {run_id: crypto.randomUUID(), started_at: new Date().toISOString(),
      candidates_sha256: hash(candidatesBody), candidate_count: candidates.length,
      source: ORIGIN, mode: 'live_read_local_capture', database_writes: 0, deletions: 0};
    atomicJson(manifestPath, manifest);
  }
  const queue = candidates.filter(c => {
    if (selected && !selected.has(String(c.aptem_id))) return false;
    const file = path.join(out, 'learners', String(c.aptem_id) + '.json');
    if (!fs.existsSync(file)) return true;
    const body = fs.readFileSync(file);
    if (fs.readFileSync(file + '.sha256', 'utf8') !== hash(body)) throw Error('Capture checksum mismatch');
    const saved = JSON.parse(body);
    return !['ok', 'no_current_program'].includes(saved.status)
      || saved.document_errors?.length > 0 || !!saved.signature_error;
  });
  manifest = {...manifest, state: 'running', pid: process.pid, workers: options.workers,
    expected_learner_ids: candidates.filter(c => !selected || selected.has(String(c.aptem_id))).map(c => String(c.aptem_id)),
    queued: queue.length, completed: 0, updated_at: new Date().toISOString()};
  atomicJson(manifestPath, manifest);
  // Installed Google Chrome by default. On a server without Chrome (e.g. ARM VPS)
  // set APTEM_BROWSER_CHANNEL= (empty) to use Playwright's bundled Chromium.
  const channel = process.env.APTEM_BROWSER_CHANNEL ?? 'chrome';
  const browser = await chromium.launch({headless: true, ...(channel ? {channel} : {})});
  const context = await browser.newContext({userAgent: 'Mozilla/5.0'});
  let requests = 0, retries = 0, completed = 0, cursor = 0;
  const started = Date.now();
  let fatalAuth = false, stopRequested = false;
  try {
    const html = await (await context.request.get(ORIGIN + '/Users/Account/LogOn', {timeout: 30000})).text();
    const form = {};
    for (const tag of html.match(/<input\b[^>]*>/gi) || []) {
      const name = tag.match(/\bname=["']([^"']+)["']/i), value = tag.match(/\bvalue=["']([^"']*)["']/i);
      if (name) form[name[1]] = value ? value[1] : '';
    }
    form.userNameOrEmail = credentials.APTEM_LOGIN_EMAIL;
    form.password = credentials.APTEM_LOGIN_PASSWORD;
    const login = await context.request.post(ORIGIN + '/Users/Account/LogOn', {form, maxRedirects: 0, timeout: 30000});
    if (login.status() !== 302) throw Error('Aptem login failed');
    await login.dispose();
    async function list(kind, values = {}) {
      const items = [], pages = []; let total;
      for (let page = 1; page <= 100; page++) {
        if (fatalAuth) throw Error('Aptem authentication expired');
        const params = {sort: '', group: '', filter: '', page: String(page), pageSize: '100',
          skip: String((page - 1) * 100), take: '100', ...values};
        let data;
        for (let attempt = 0; attempt < 3; attempt++) {
          await delay(80);
          let response;
          try {
            requests++;
            const requestOptions = {timeout: 30000, maxRedirects: 0,
              headers: {'X-Requested-With': 'XMLHttpRequest', Accept: 'application/json'}};
            response = kind === 'modern'
              ? await context.request.get(ORIGIN + ENDPOINTS[kind] + '?' + new URLSearchParams(params), requestOptions)
              : await context.request.post(ORIGIN + ENDPOINTS[kind], {...requestOptions, form: params});
            const status = response.status();
            if ([301, 302, 401, 403].includes(status)) { fatalAuth = true; throw Error('Aptem authentication expired'); }
            if ([408, 429, 500, 502, 503, 504].includes(status) && attempt < 2) {
              retries++; await delay(Math.min(30000, Number(response.headers()['retry-after'] || 2 ** attempt) * 1000)); continue;
            }
            if (status !== 200) throw Error(kind + ': HTTP ' + status);
            data = await response.json();
            if (!Array.isArray(data.Data) || !Number.isInteger(data.Total) || data.Total < 0 || !emptyErrors(data.Errors))
              throw Error(kind + ': invalid list response');
            break;
          } catch (error) {
            if (error.name === 'TimeoutError' && attempt < 2) { retries++; await delay(1000 * 2 ** attempt); continue; }
            throw Error(kind + ': ' + (error.name === 'TimeoutError' ? 'request timed out' : error.message.split('\n')[0]));
          } finally { if (response) await response.dispose(); }
        }
        if (!data) throw Error(kind + ': retries exhausted');
        if (total === undefined) total = data.Total;
        if (data.Total !== total) throw Error(kind + ': inventory changed while paging');
        items.push(...data.Data);
        pages.push({page, count: data.Data.length, http_status: 200, source_total: total});
        if (items.length === total) return {complete: true, total, pages, items};
        if (!data.Data.length || items.length > total) throw Error(kind + ': incomplete pagination');
      }
      throw Error(kind + ': page limit exceeded');
    }
    async function profile(candidate) {
      const user = String(candidate.aptem_id);
      const result = {...candidate, run_id: manifest.run_id, started_at: new Date().toISOString(),
        source: ORIGIN, status: 'error', documents: {}, document_errors: [], groups_by_program: []};
      try {
        result.episodes_bucket = await list('episodes', {userId: user});
        if (result.episodes_bucket.items.some(r => String(r.UserId) !== user)) throw Error('Programme learner mismatch');
        const current = result.episodes_bucket.items.filter(r => r.IsCurrent === true && r.ParentProgramId === 0);
        let program = current.length === 1 ? String(current[0].ProgramId) : null;
        if (!program) {
          const response = await context.request.get(ORIGIN + '/MWS.ClientAdmin/JobSeekerProfile/Index/' + user,
            {timeout: 30000, maxRedirects: 0});
          try {
            if (response.status() !== 200) throw Error('Current programme profile unavailable');
            const body = await response.text();
            const tag = (body.match(/<input\b[^>]*>/gi) || []).find(t => /class=["'][^"']*\bcurrentProgramId\b/.test(t));
            const value = tag?.match(/\bvalue=["']([^"']*)["']/i)?.[1];
            if (value === '') { result.status = 'no_current_program'; return result; }
            if (!/^[1-9][0-9]*$/.test(value || '')) throw Error('Current programme unverified');
            program = value;
          } finally { await response.dispose(); }
        }
        result.aptem_program_id = program;
        result.modern_buckets = {};
        for (const tab of ['planned', 'finished']) {
          const filter = tab === 'planned'
            ? "(status~eq~'Not Scheduled'~or~status~eq~'Scheduled'~or~status~eq~'In Progress')"
            : "(status~eq~'Awaiting Signature'~or~status~eq~'Completed'~or~status~eq~'Archived'~or~status~eq~'Not Completed')";
          const bucket = reviewScope(await list('modern', {filter, userId: user, programId: program}));
          if (bucket.items.some(r => String(r.UserId) !== user)) throw Error('Modern learner mismatch');
          result.modern_buckets[tab] = bucket;
          result[tab] = bucket.items.map(r => ({id: r.Id, type: r.TypeName, status: r.Status, tab}));
        }
        const ids = [...result.planned, ...result.finished].map(r => String(r.id));
        if (new Set(ids).size !== ids.length || ids.some(id => !/^[1-9][0-9]*$/.test(id))) throw Error('Invalid or repeated Modern identity');
        result.membership_checked_at = new Date().toISOString();
        try {
          result.signatures_bucket = await list('signatures', {sort: 'DocumentId-asc', filter: 'EntityId~eq~' + user});
          if (result.signatures_bucket.items.some(r => String(r.EntityId) !== user)) throw Error('Signature learner mismatch');
          if (new Set(result.signatures_bucket.items.map(r => r.DocumentId)).size !== result.signatures_bucket.total)
            throw Error('Duplicate signature document identity');
          result.signatures_checked_at = new Date().toISOString();
        } catch (error) { result.signature_error = error.message; delete result.signatures_bucket; }
        const programs = [...new Set([program, ...result.episodes_bucket.items.map(r => String(r.ProgramId))])];
        for (const pid of programs) {
          if (fatalAuth) break;
          try {
            const bucket = await list('groups', {userId: user, programId: pid});
            if (bucket.items.some(r => String(r.ProgramId) !== pid)) throw Error('Classic group programme mismatch');
            result.groups_by_program.push({program_id: pid, ...bucket});
          } catch (error) { result.document_errors.push({program_id: pid, error: error.message}); }
        }
        result.groups = result.groups_by_program.flatMap(b => b.items).map(g => ({review_id: g.ItemId,
          item_type: g.ItemType, program_id: g.ProgramId, document_count: g.DocCount,
          fully_signed: g.IsFullySigned, should_sign: g.ShouldSign}));
        for (const rid of ids) {
          if (fatalAuth) break;
          const groups = result.groups_by_program.flatMap(b => b.items).filter(g => String(g.ItemId) === rid && g.ItemType === 9);
          if (groups.length !== 1) continue;
          try {
            const bucket = await list('documents', {userId: user, itemId: rid, itemType: '9', sort: 'CreateDateTime-desc'});
            const docids = bucket.items.map(d => String(d.Id));
            if (new Set(docids).size !== docids.length) throw Error('Duplicate document identity');
            result.documents[rid] = {...bucket, source_sort: 'CreateDateTime-desc', checked_at: new Date().toISOString(),
              items: bucket.items.map(d => ({...d, source_url: ORIGIN + '/MWS.eDocuments/DynamicDocuments/GetFile?documentId=' + d.Id}))};
          } catch (error) { result.document_errors.push({review_id: rid, error: error.message}); }
        }
        result.status = 'ok';
      } catch (error) { result.error = String(error.message).slice(0, 200); }
      finally { result.checked_at = new Date().toISOString(); }
      return result;
    }
    async function worker(workerId) {
      while (cursor < queue.length && !fatalAuth && !stopRequested) {
        const candidate = queue[cursor++];
        const result = await profile(candidate);
        atomicJson(path.join(out, 'learners', String(candidate.aptem_id) + '.json'), result);
        completed++;
        const progress = {worker_id: workerId, pid: process.pid, completed, queued: queue.length, learner: candidate.aptem_id, status: result.status,
          planned: result.planned?.length, finished: result.finished?.length,
          document_errors: result.document_errors.length, error: result.error,
          requests, retries, elapsed_seconds: Math.round((Date.now() - started) / 1000)};
        fs.appendFileSync(path.join(out, 'progress.jsonl'), JSON.stringify(progress) + '\n');
        manifest = {...manifest, ...progress, updated_at: new Date().toISOString()};
        atomicJson(manifestPath, manifest);
        if (completed % 10 === 0 || selected || result.status === 'error') console.log(JSON.stringify(progress));
      }
    }
    console.log(JSON.stringify({started: true, queued: queue.length, workers: options.workers, output: out}));
    const workerResults = await Promise.allSettled(Array.from({length: options.workers}, (_, index) =>
      worker(index + 1).catch(error => { stopRequested = true; throw error; })));
    const failedWorker = workerResults.find(result => result.status === 'rejected');
    if (failedWorker) throw failedWorker.reason;
    if (options['verify-ui'] === 'true') {
      const proof = [];
      const page = await context.newPage();
      try {
        for (const user of (options['ui-learners'] || '106,16098,4907').split(',')) {
          if (selected && !selected.has(user)) continue;
          const capture = JSON.parse(fs.readFileSync(path.join(out, 'learners', user + '.json')));
          if (capture.status !== 'ok') throw Error('UI sample has no successful inventory: ' + user);
          for (const tab of ['planned', 'finished']) {
            const responsePromise = page.waitForResponse(r => new URL(r.url()).pathname === ENDPOINTS.modern, {timeout: 45000});
            await page.goto(ORIGIN + '/pwa/learners/' + user + '/reviews/' + (tab === 'planned' ? 'upcoming' : tab),
              {waitUntil: 'domcontentloaded', timeout: 45000});
            const response = await responsePromise;
            const url = new URL(response.url());
            const data = await response.json();
            const expected = new Set(capture[tab].map(r => String(r.id)));
            if (response.status() !== 200 || data.Total !== capture.modern_buckets[tab].source_total || !emptyErrors(data.Errors)
                || String(url.searchParams.get('programId')) !== String(capture.aptem_program_id)
                || data.Data.some(r => !/enrol/i.test(String(r.TypeName || '')) && !expected.has(String(r.Id)))) throw Error('Live UI comparison differs for learner ' + user + ' ' + tab);
            // Follow the UI's own request/filter through all pages, then compare exact IDs.
            const observed = reviewScope(await list('modern', Object.fromEntries([...url.searchParams].filter(([k]) =>
              !['page', 'pageSize', 'skip', 'take'].includes(k)))));
            const actual = new Set(observed.items.map(r => String(r.Id)));
            if (observed.total !== expected.size || actual.size !== expected.size || [...actual].some(id => !expected.has(id)))
              throw Error('Paginated UI comparison differs for learner ' + user + ' ' + tab);
            proof.push({aptem_learner_id: user, tab, checked_at: new Date().toISOString(),
              total: observed.total, exact_ids_match: true, observed_query: Object.fromEntries(url.searchParams), pages: observed.pages});
          }
        }
        atomicJson(path.join(out, 'ui-comparison.json'), proof);
        console.log(JSON.stringify({ui_samples_verified: proof.length / 2, tabs_verified: proof.length, exact_ids_match: true}));
      } finally { await page.close(); }
    }
    atomicJson(path.join(out, 'last-invocation.json'), {finished_at: new Date().toISOString(), completed,
      queued: queue.length, requests, retries, fatal_auth: fatalAuth, database_writes: 0, deletions: 0});
    if (fatalAuth) throw Error('Capture paused because Aptem authentication expired');
    atomicJson(manifestPath, {...manifest, state: 'complete', completed, updated_at: new Date().toISOString()});
    console.log(JSON.stringify({complete: true, completed, requests, retries, output: out}));
  } catch (error) {
    atomicJson(manifestPath, {...manifest, state: 'failed', error: String(error.message).split('\n')[0].slice(0, 200), updated_at: new Date().toISOString()});
    throw error;
  } finally { await browser.close(); }
}
if (require.main === module) main().catch(error => {console.error(error.message.split('\n')[0].slice(0, 250)); process.exitCode = 1;});
module.exports = {args, emptyErrors, atomicJson, reviewScope};
