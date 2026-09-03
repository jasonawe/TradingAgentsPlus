import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const root = process.cwd();
const ua = path.join(root, '.ua');
const batches = JSON.parse(fs.readFileSync(path.join(ua, 'intermediate/batches.json'), 'utf8'));
const extractor = '/Users/shenkang/.codex/plugins/cache/understand-anything/understand-anything/2.9.4/skills/understand/extract-structure.mjs';
const temp = path.join(ua, 'tmp');
fs.mkdirSync(temp, { recursive: true });

function typeFor(f) {
  if (f.fileCategory === 'config') return 'config';
  if (f.fileCategory === 'docs') return 'document';
  if (f.fileCategory === 'infra') return f.path.includes('.github/workflows') ? 'pipeline' : 'service';
  if (f.fileCategory === 'data') return f.path.endsWith('.sql') ? 'table' : 'schema';
  return 'file';
}
function complexity(lines, count=0) { return lines > 200 || count > 12 ? 'complex' : lines >= 50 || count > 3 ? 'moderate' : 'simple'; }
function tags(f, r) {
  const p = f.path.toLowerCase(); const out = [];
  if (f.fileCategory === 'docs') out.push('documentation');
  if (f.fileCategory === 'config') out.push('configuration');
  if (f.fileCategory === 'infra') out.push(p.includes('workflow') ? 'ci-cd' : 'infrastructure');
  if (f.fileCategory === 'data') out.push('database', 'migration');
  if (p.includes('test')) out.push('test');
  if (p.endsWith('.html') || p.endsWith('.css') || p.includes('static/')) out.push('frontend');
  if (p.includes('readme')) out.push('entry-point');
  if (p.includes('plan')) out.push('planning');
  if (p.includes('spec')) out.push('design-spec');
  if (p.includes('env')) out.push('environment');
  if (p.endsWith('.json')) out.push('metadata');
  if (p.endsWith('.sql')) out.push('schema-definition');
  if (r?.functions?.length || r?.classes?.length) out.push('implementation');
  const unique = [...new Set(out)];
  for (const fallback of ['project-artifact', 'tradingagents', 'reference']) {
    if (unique.length >= 3) break;
    if (!unique.includes(fallback)) unique.push(fallback);
  }
  return unique.slice(0, 5);
}
function summary(f, r, text) {
  const p = f.path;
  if (p === '.github/workflows/ci.yml') return 'GitHub Actions 持续集成工作流，负责安装依赖、执行测试与静态检查，并在代码变更时验证项目质量。';
  if (f.fileCategory === 'data') return `数据库迁移文件 ${path.basename(p)}，定义或调整 Web 平台的持久化表结构、索引与可靠性相关字段。`;
  if (f.fileCategory === 'config') return `项目配置文件 ${path.basename(p)}，集中描述运行环境、工具链或 Understand 中间产物所需的结构化设置。`;
  if (f.fileCategory === 'docs') {
    if (p === 'README.md') return '项目入口文档，介绍 TradingAgents 的定位、安装配置、运行方式与主要能力。';
    if (p === 'CHANGELOG.md') return '版本变更日志，按时间记录功能迭代、缺陷修复和兼容性调整。';
    return `设计或实施计划文档，围绕 ${path.basename(p, path.extname(p))} 描述目标、方案、任务拆分与验证要求。`;
  }
  if (p.endsWith('.html')) return `前端 HTML 资源，用于展示 ${path.basename(p, '.html')} 相关的交互原型或应用界面结构。`;
  if (p.endsWith('.css')) return '前端样式表，定义 Web 控制台的布局、颜色、排版与交互状态。';
  if (p.endsWith('.js')) return `前端或脚本模块 ${path.basename(p)}，实现页面交互、数据刷新、图表展示或本地化逻辑。`;
  return `项目文件 ${p}，承载与 TradingAgents 分析平台相关的结构化内容或实现逻辑。`;
}
function addChildNodes(nodes, edges, f, r) {
  for (const fn of (r.functions || [])) {
    const lines = (fn.endLine || fn.startLine) - (fn.startLine || 1) + 1;
    if (lines < 10 && !(r.exports || []).some(e => e.name === fn.name)) continue;
    const id = `function:${f.path}:${fn.name}`;
    nodes.push({ id, type:'function', name:fn.name, filePath:f.path, lineRange:[fn.startLine, fn.endLine], summary:`实现 ${fn.name}，负责该模块中的具体处理流程。`, tags:['implementation','function','project-artifact'], complexity:complexity(lines) });
    edges.push({source:`file:${f.path}`,target:id,type:'contains',direction:'forward',weight:1.0});
    if ((r.exports || []).some(e => e.name === fn.name)) edges.push({source:`file:${f.path}`,target:id,type:'exports',direction:'forward',weight:0.8});
  }
  for (const cl of (r.classes || [])) {
    const lines = (cl.endLine || cl.startLine) - (cl.startLine || 1) + 1;
    if (lines < 20 && (cl.methods || []).length < 2 && !(r.exports || []).some(e => e.name === cl.name)) continue;
    const id = `class:${f.path}:${cl.name}`;
    nodes.push({ id, type:'class', name:cl.name, filePath:f.path, lineRange:[cl.startLine, cl.endLine], summary:`封装 ${cl.name}，组织该模块的相关状态与行为。`, tags:['implementation','class','project-artifact'], complexity:complexity(lines) });
    edges.push({source:`file:${f.path}`,target:id,type:'contains',direction:'forward',weight:1.0});
    if ((r.exports || []).some(e => e.name === cl.name)) edges.push({source:`file:${f.path}`,target:id,type:'exports',direction:'forward',weight:0.8});
  }
}

for (const b of batches.batches.filter(x => x.batchIndex >= 10 && x.batchIndex <= 19)) {
  const inputPath = path.join(temp, `ua-file-analyzer-input-${b.batchIndex}.json`);
  const resultPath = path.join(temp, `ua-file-extract-results-${b.batchIndex}.json`);
  fs.writeFileSync(inputPath, JSON.stringify({ projectRoot: root, batchFiles: b.files, batchImportData: b.batchImportData }, null, 2));
  execFileSync('node', [extractor, inputPath, resultPath], { cwd: root, stdio: 'inherit' });
  const extraction = JSON.parse(fs.readFileSync(resultPath, 'utf8'));
  const byPath = new Map((extraction.results || []).map(r => [r.path, r]));
  const nodes = [], edges = [];
  for (const f of b.files) {
    const r = byPath.get(f.path) || {};
    const t = typeFor(f); const id = `${t}:${f.path}`;
    const structuralCount = (r.functions || []).length + (r.classes || []).length;
    nodes.push({ id, type:t, name:path.basename(f.path), filePath:f.path, summary:summary(f, r, ''), tags:tags(f, r), complexity:complexity(r.nonEmptyLines || f.sizeLines, structuralCount) });
    for (const target of (b.batchImportData[f.path] || [])) edges.push({source:`file:${f.path}`,target:`file:${target}`,type:'imports',direction:'forward',weight:0.7});
    addChildNodes(nodes, edges, f, r);
  }
  for (const f of b.files) {
    const id = `${typeFor(f)}:${f.path}`;
    if (f.fileCategory === 'infra') {
      for (const target of ['README.md', 'main.py']) if (b.files.some(x => x.path === target)) edges.push({source:id,target:`file:${target}`,type:'triggers',direction:'forward',weight:0.6});
    }
    if (f.fileCategory === 'data') {
      const target = b.files.find(x => x.path !== f.path && x.fileCategory === 'data');
      if (target) edges.push({source:id,target:`table:${target.path}`,type:'migrates',direction:'forward',weight:0.7});
    }
  }
  const outPath = path.join(ua, 'intermediate', `batch-${b.batchIndex}.json`);
  fs.writeFileSync(outPath, JSON.stringify({nodes, edges}, null, 2) + '\n');
  console.error(`batch-${b.batchIndex}: ${nodes.length} nodes, ${edges.length} edges`);
}
