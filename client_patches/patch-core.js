/**
 * patch-core.js
 * 核心补丁执行器：对解包后的 asar 源码执行结构化 AST/正则替换。
 *
 * 包含两个核心 Patch：
 * 1. Phase 1 (防篡改): 从 config/batchWrite 的 keypath 数组中剔除 features.js_repl
 * 2. Phase 2 (能力赋能): 在全局特征仲裁入口 Er() 中强制注入桌面高级能力，突破自定义模型 (model_provider = "custom") / Statsig 门控
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function sha256(content) {
  return crypto.createHash('sha256').update(content, 'utf8').digest('hex').toUpperCase();
}

function findJsFiles(dir) {
  const results = [];
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== 'node_modules') {
        results.push(...findJsFiles(fullPath));
      }
    } else if (entry.isFile() && entry.name.endsWith('.js')) {
      results.push(fullPath);
    }
  }
  return results;
}

function runPatch(extractDir, logFilePath) {
  if (!fs.existsSync(extractDir)) {
    throw new Error(`解包目录不存在: ${extractDir}`);
  }

  const jsFiles = findJsFiles(extractDir);
  const logEntries = [];
  let totalKeypathPatched = 0;
  let totalErPatched = 0;

  // Regex 1: Keypath 数组首元素 features.js_repl
  const keypathPattern = /\[([`'"])features\.js_repl\1\s*,\s*(?=[`'"]mcp_servers\.)/g;

  // Regex 2: Er() 结构化特征仲裁函数定位
  // 兼容反编译变量名混淆 (e, t, n, r, i, o, s, ar, Ree 等任意命名)
  const erPattern = /function\s+([A-Za-z0-9_$]+)\s*\(\s*([A-Za-z0-9_$]+)\s*,\s*\{\s*buildFlavor\s*:\s*([A-Za-z0-9_$]+)\s*=\s*([^,]+),\s*env\s*:\s*([A-Za-z0-9_$]+)\s*=\s*([^,]+),\s*platform\s*:\s*([A-Za-z0-9_$]+)\s*=\s*([^}]+)\}\s*=\s*\{\}\s*\)\s*\{let\s+([A-Za-z0-9_$]+)=\7===([`'"])win32\10&&[^;]+;return\s+([A-Za-z0-9_$]+)==null\?\{\.\.\.([A-Za-z0-9_$]+),deviceAttestation:([A-Za-z0-9_$]+)\(\{platform:\7\}\)\}:\{\.\.\.\12,\.\.\.\11,deviceAttestation:\13\(\{platform:\7\}\)\}\}/g;

  for (const file of jsFiles) {
    let content = fs.readFileSync(file, 'utf8');
    const relPath = path.relative(extractDir, file).replace(/\\/g, '/');
    let fileModified = false;
    const shaBefore = sha256(content);
    let patch1Count = 0;
    let patch2Count = 0;
    let snippetBefore1 = '', snippetAfter1 = '';
    let snippetBefore2 = '', snippetAfter2 = '';

    // 执行 Patch 1 (Keypath 过滤)
    if (content.includes('features.js_repl')) {
      const matches1 = [...content.matchAll(keypathPattern)];
      if (matches1.length > 0) {
        patch1Count = matches1.length;
        const snipIdx = matches1[0].index;
        snippetBefore1 = content.substring(Math.max(0, snipIdx - 40), snipIdx + 80);
        content = content.replace(keypathPattern, '[');
        snippetAfter1 = content.substring(Math.max(0, snipIdx - 40), snipIdx + 60);
        fileModified = true;
        totalKeypathPatched += patch1Count;
      }
    }

    // 执行 Patch 2 (Er 仲裁强制赋能)
    if (content.includes('CODEX_ELECTRON_ENABLE_WINDOWS_COMPUTER_USE')) {
      const matches2 = [...content.matchAll(erPattern)];
      if (matches2.length > 0) {
        patch2Count = matches2.length;
        const m = matches2[0];
        snippetBefore2 = m[0];
        const [full, fnName, argE, varT, defT, varEnv, defEnv, varPlat, defPlat, varI, quote, varS, varO, attestFn] = m;

        // 动态探测 Ree 函数名
        const reeMatch = full.match(/\?([A-Za-z0-9_$]+)\(\w+\):null/);
        const reeFn = reeMatch ? reeMatch[1] : 'Ree';

        // 构造强制赋能逻辑：
        // 注入 inAppBrowserUse, inAppBrowserUseAllowed, browserPane, externalBrowserUse, externalBrowserUseAllowed, computerUse, computerUseNodeRepl
        const replacement = `function ${fnName}(${argE},{buildFlavor:${varT}=${defT},env:${varEnv}=${defEnv},platform:${varPlat}=${defPlat}}={}){let k={...${argE},inAppBrowserUse:!0,inAppBrowserUseAllowed:!0,browserPane:!0,externalBrowserUse:!0,externalBrowserUseAllowed:!0,computerUse:!0,computerUseNodeRepl:!0};let ${varI}=${varPlat}===\`win32\`&&k.computerUse===!0?{...k,computerUseNodeRepl:!0}:k,${varO}=${varPlat}===\`win32\`&&${varEnv}.CODEX_ELECTRON_ENABLE_WINDOWS_COMPUTER_USE===\`1\`?{...${varI},computerUse:!0,computerUseNodeRepl:!0}:${varI},${varS}=${reeFn}(${varEnv});return ${varS}==null?{...${varO},deviceAttestation:${attestFn}({platform:${varPlat}})}:{...${varO},...${varS},deviceAttestation:${attestFn}({platform:${varPlat}})}}`;

        content = content.replace(erPattern, replacement);
        snippetAfter2 = replacement;
        fileModified = true;
        totalErPatched += patch2Count;
      }
    }

    if (fileModified) {
      const shaAfter = sha256(content);
      fs.writeFileSync(file, content, 'utf8');
      logEntries.push({
        file: relPath,
        sha256Before: shaBefore,
        sha256After: shaAfter,
        phase1_KeypathPatches: patch1Count,
        phase2_ErPatches: patch2Count,
        snippetBefore1,
        snippetAfter1,
        snippetBefore2,
        snippetAfter2
      });
      console.log(`[PATCH SUCCESS] ${relPath} -> Phase1(Keypath): ${patch1Count}, Phase2(Er): ${patch2Count}`);
    }
  }

  // 计数断言
  if (totalKeypathPatched === 0) {
    throw new Error('未找到 Phase 1 (features.js_repl keypath) patch 点，结构可能已变化');
  }
  if (totalErPatched === 0) {
    throw new Error('未找到 Phase 2 (Er feature gate) patch 点，结构可能已变化');
  }

  const report = {
    timestamp: new Date().toISOString(),
    totalKeypathPatched,
    totalErPatched,
    files: logEntries
  };

  if (logFilePath) {
    fs.writeFileSync(logFilePath, JSON.stringify(report, null, 2), 'utf8');
  }

  console.log(`补丁完成: Keypath 点数=${totalKeypathPatched}, Er 赋能点数=${totalErPatched}`);
  return report;
}

if (require.main === module) {
  const extractDir = process.argv[2];
  const logFile = process.argv[3];
  if (!extractDir) {
    console.error('用法: node patch-core.js <extractDir> [logFile]');
    process.exit(1);
  }
  try {
    runPatch(extractDir, logFile);
  } catch (err) {
    console.error('[PATCH ERROR]', err.message);
    process.exit(1);
  }
}

module.exports = { runPatch };
