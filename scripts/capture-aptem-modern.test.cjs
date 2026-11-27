const test = require('node:test');
const assert = require('node:assert/strict');
const {args, reviewScope, atomicJson} = require('./capture-aptem-modern.cjs');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');

test('Enrollment exclusion preserves pagination evidence and other review types', () => {
  const source = {complete: true, total: 4, items: [
    {Id: 1, TypeName: 'Progress Review'}, {Id: 2, TypeName: 'Enrollment'},
    {Id: 3, TypeName: 'Monthly Coaching Meeting'}, {Id: 4, TypeName: 'Enrolment form'}],
    pages: [{page: 1, count: 2, http_status: 200}, {page: 2, count: 2, http_status: 200}]};
  const result = reviewScope(source);
  assert.equal(result.total, 2);
  assert.equal(result.source_total, 4);
  assert.deepEqual(result.items.map(row => row.Id), [1, 3]);
  assert.deepEqual(result.excluded_enrollment_ids, ['2', '4']);
  assert.deepEqual(result.pages.map(row => [row.count, row.source_count]), [[1, 2], [1, 2]]);
  assert.equal(source.items.length, 4);
});

test('worker parallelism is bounded and defaults to three', () => {
  const required = ['--credentials', 'credentials.env', '--candidates', 'candidates.json', '--output', 'capture'];
  assert.equal(args(required).workers, 3);
  assert.throws(() => args([...required, '--workers', '20']), /1 to 4/);
});

test('Windows sharing violation retries publication without losing the checkpoint', () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'modern-capture-test-'));
  const target = path.join(folder, 'manifest.json');
  const rename = fs.renameSync;
  let attempts = 0;
  try {
    fs.renameSync = (...values) => {
      if (++attempts < 3) throw Object.assign(new Error('file being read'), {code: 'EPERM'});
      return rename(...values);
    };
    atomicJson(target, {completed: 345});
    assert.equal(attempts, 3);
    assert.deepEqual(JSON.parse(fs.readFileSync(target)), {completed: 345});
    assert.equal(fs.readFileSync(target + '.sha256', 'utf8'),
      crypto.createHash('sha256').update(fs.readFileSync(target)).digest('hex'));
  } finally {
    fs.renameSync = rename;
    fs.rmSync(folder, {recursive: true});
  }
});
