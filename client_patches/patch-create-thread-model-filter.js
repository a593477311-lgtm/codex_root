/**
 * patch-create-thread-model-filter.js
 *
 * Aligns create_thread's runtime model filter with the schema-side filter for
 * custom model providers.  The schema generator allows custom providers to
 * bypass the Statsig hidden-model allowlist; Q3o previously omitted that
 * bypass.  This patch computes the same predicate immediately before filtering
 * and does not alter $Yo or any other validation logic.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const OLD_FILTER = [
  'async function Q3o(e,t){let n;try{n=await Kni(n=>Hg(e,t).sendRequest(`model/list`,{...n},{priority:`critical`}))}catch{return null}',
  'if(t!==`local`)return n.filter(e=>!e.hidden);',
  'let r;try{r=await kri(e,t)}catch{return n.filter(e=>!e.hidden)}',
  'let i=e.get(kza);return n.filter(e=>i.useHiddenModels&&r!==`amazonBedrock`?i.availableModels.has(e.model):!e.hidden)}',
].join('');

const NEW_FILTER = [
  'async function Q3o(e,t){let n;try{n=await Kni(n=>Hg(e,t).sendRequest(`model/list`,{...n},{priority:`critical`}))}catch{return null}',
  'if(t!==`local`)return n.filter(e=>!e.hidden);',
  'let r;try{r=await kri(e,t)}catch{return n.filter(e=>!e.hidden)}',
  'let i=e.get(kza),a=!1;try{a=wRt(S_((await Hg(e,t).sendRequest(`config/read`,{includeLayers:!1,cwd:null})).config))}catch{}',
  'return n.filter(e=>i.useHiddenModels&&r!==`amazonBedrock`&&!a?i.availableModels.has(e.model):!e.hidden)}',
].join('');

function sha256(content) {
  return crypto.createHash('sha256').update(content, 'utf8').digest('hex').toUpperCase();
}

function countOrdinal(haystack, needle) {
  let count = 0;
  for (let pos = 0; (pos = haystack.indexOf(needle, pos)) !== -1; pos += needle.length) {
    count++;
  }
  return count;
}

function findJsFiles(dir) {
  const results = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== 'node_modules') results.push(...findJsFiles(fullPath));
    } else if (entry.isFile() && entry.name.endsWith('.js')) {
      results.push(fullPath);
    }
  }
  return results;
}

function runPatch(extractDir) {
  if (!fs.existsSync(extractDir)) throw new Error(`解包目录不存在: ${extractDir}`);

  const candidates = [];
  let oldTotal = 0;
  let newTotal = 0;

  for (const file of findJsFiles(extractDir)) {
    const content = fs.readFileSync(file, 'utf8');
    const oldCount = countOrdinal(content, OLD_FILTER);
    const newCount = countOrdinal(content, NEW_FILTER);
    if (oldCount || newCount) {
      candidates.push({
        file,
        relativePath: path.relative(extractDir, file).replace(/\\/g, '/'),
        content,
        oldCount,
        newCount,
      });
    }
    oldTotal += oldCount;
    newTotal += newCount;
  }

  if (oldTotal === 0 && newTotal === 1) {
    if (candidates.length !== 1) throw new Error(`补丁形态命中异常: files=${candidates.length}`);
    const target = candidates[0];
    return {
      status: 'already-patched',
      relativePath: target.relativePath,
      oldCount: 0,
      newCount: 1,
      sha256: sha256(target.content),
    };
  }

  if (oldTotal !== 1 || newTotal !== 0) {
    throw new Error(`锚点不唯一或形态异常: old=${oldTotal}, new=${newTotal}`);
  }
  if (candidates.length !== 1) {
    throw new Error(`锚点跨文件命中异常: files=${candidates.length}`);
  }

  const target = candidates[0];
  const sha256Before = sha256(target.content);
  const patched = target.content.replace(OLD_FILTER, NEW_FILTER);
  if (countOrdinal(patched, NEW_FILTER) !== 1 || countOrdinal(patched, OLD_FILTER) !== 0) {
    throw new Error('替换后锚点校验失败');
  }
  fs.writeFileSync(target.file, patched, 'utf8');

  return {
    status: 'patched',
    relativePath: target.relativePath,
    oldCount: 0,
    newCount: 1,
    sha256Before,
    sha256After: sha256(patched),
  };
}

if (require.main === module) {
  const extractDir = process.argv[2];
  if (!extractDir) {
    console.error('用法: node patch-create-thread-model-filter.js <extractDir>');
    process.exit(1);
  }
  try {
    console.log(JSON.stringify(runPatch(extractDir), null, 2));
  } catch (err) {
    console.error('[PATCH ERROR]', err.message);
    process.exit(1);
  }
}

module.exports = { runPatch, OLD_FILTER, NEW_FILTER };
