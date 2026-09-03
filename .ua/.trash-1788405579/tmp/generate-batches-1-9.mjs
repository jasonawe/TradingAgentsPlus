import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const intermediate = path.join(root, '.ua', 'intermediate');
const batches = JSON.parse(fs.readFileSync(path.join(intermediate, 'batches.json'), 'utf8'));

const categoryType = { code: 'file', script: 'file', markup: 'file', config: 'config', docs: 'document', data: 'schema', infra: 'service' };
const typeTag = { code: '代码实现', script: '脚本', markup: '标记文件', config: '配置', docs: '文档', data: '数据结构', infra: '基础设施' };
const chineseTags = {
  test: '测试', utility: '工具函数', service: '服务', validation: '校验', schema: '模式定义', entry: '入口点', data: '数据流', agent: '智能体', graph: '图编排', web: 'Web', provider: '模型提供商', config: '配置', infrastructure: '基础设施', container: '容器化', orchestration: '编排', api: '接口', error: '错误处理', persistence: '持久化', market: '市场数据', llm: '大模型'
};

function complexity(r) {
  const n = r.nonEmptyLines ?? r.totalLines ?? 0;
  const defs = (r.functions?.length ?? 0) + (r.classes?.length ?? 0);
  return n > 200 || defs > 12 ? 'complex' : n >= 50 || defs > 3 ? 'moderate' : 'simple';
}
function tagsFor(file, r) {
  const p = file.path.toLowerCase();
  const out = [];
  if (p.includes('test') || p.includes('spec')) out.push('test');
  if (p.includes('agent')) out.push('agent');
  if (p.includes('graph')) out.push('graph');
  if (p.includes('web')) out.push('web');
  if (p.includes('provider') || p.includes('llm') || p.includes('model')) out.push('provider');
  if (p.includes('data') || p.includes('finance') || p.includes('market') || p.includes('stock') || p.includes('y_finance')) out.push('market');
  if (p.includes('schema') || p.includes('models')) out.push('schema');
  if (p.includes('error') || p.includes('validator') || p.includes('validation')) out.push('validation');
  if (p.endsWith('__init__.py') || p.endsWith('/main.py') || p.endsWith('/main.go')) out.push('entry');
  if (file.fileCategory === 'config') out.push('config');
  if (file.fileCategory === 'infra') out.push('infrastructure');
  if (!out.length) out.push('utility');
  out.push(typeTag[file.fileCategory] || '모듈');
  return [...new Set(out.map(x => chineseTags[x] || x))].slice(0, 5);
}
function fileSummary(file, r) {
  const p = file.path;
  if (file.fileCategory === 'infra') return `定义 ${p} 的部署与运行环境，负责项目服务的构建、启动或编排。`;
  if (file.fileCategory === 'config') return `集中配置项目运行参数与构建行为，为相关模块提供一致的环境设置。`;
  if (file.fileCategory === 'docs') return `说明项目的使用方式、设计约定或开发流程，为维护者和用户提供参考。`;
  if (p.includes('/tests/') || p.startsWith('tests/')) return `覆盖 ${p.replace(/^tests\//, '')} 相关行为的自动化测试，验证边界条件与回归场景。`;
  const names = [...(r.exports || [])].map(x => x.name).filter(Boolean).slice(0, 3);
  const subject = names.length ? `，对外提供 ${names.join('、')} 等接口` : '';
  return `实现 ${p} 在交易分析系统中的核心逻辑${subject}，并负责相关数据或流程的协调。`;
}
function symbolSummary(file, name, kind) {
  const base = kind === 'class' ? '类' : '函数';
  return `位于 ${file.path} 的${base} ${name}，承担该模块中的专门职责并被测试或其他流程使用。`;
}
function idFor(file, r) { return `${categoryType[file.fileCategory] || 'file'}:${file.path}`; }

for (let batchIndex = 1; batchIndex <= 9; batchIndex++) {
  const batch = batches.batches.find(b => b.batchIndex === batchIndex);
  const extracts = JSON.parse(fs.readFileSync(path.join(root, '.ua', 'tmp', `ua-file-extract-results-${batchIndex}.json`), 'utf8'));
  const byPath = new Map(extracts.results.map(r => [r.path, r]));
  const nodes = [];
  const edges = [];
  const nodeIds = new Set();
  for (const file of batch.files) {
    const r = byPath.get(file.path) || { path: file.path, totalLines: file.sizeLines, nonEmptyLines: file.sizeLines, functions: [], classes: [], exports: [], metrics: {} };
    const fileId = idFor(file, r);
    nodes.push({ id: fileId, type: categoryType[file.fileCategory] || 'file', name: path.basename(file.path), filePath: file.path, summary: fileSummary(file, r), tags: tagsFor(file, r), complexity: complexity(r) });
    nodeIds.add(fileId);
    const exported = new Set((r.exports || []).map(x => x.name));
    for (const fn of (r.functions || [])) {
      if ((fn.endLine - fn.startLine + 1 >= 10) || exported.has(fn.name)) {
        const id = `function:${file.path}:${fn.name}`;
        if (!nodeIds.has(id)) { nodes.push({ id, type: 'function', name: fn.name, filePath: file.path, lineRange: [fn.startLine, fn.endLine], summary: symbolSummary(file, fn.name, 'function'), tags: ['函数', ...tagsFor(file, r).slice(0, 3)], complexity: fn.endLine - fn.startLine + 1 > 40 ? 'complex' : 'simple' }); nodeIds.add(id); }
      }
    }
    for (const cls of (r.classes || [])) {
      if (cls.methods?.length >= 2 || cls.endLine - cls.startLine + 1 >= 20 || exported.has(cls.name)) {
        const id = `class:${file.path}:${cls.name}`;
        if (!nodeIds.has(id)) { nodes.push({ id, type: 'class', name: cls.name, filePath: file.path, lineRange: [cls.startLine, cls.endLine], summary: symbolSummary(file, cls.name, 'class'), tags: ['类', ...tagsFor(file, r).slice(0, 3)], complexity: cls.endLine - cls.startLine + 1 > 80 ? 'complex' : 'moderate' }); nodeIds.add(id); }
      }
    }
    for (const fn of (r.functions || [])) {
      const fid = `function:${file.path}:${fn.name}`;
      if (nodeIds.has(fid)) edges.push({ source: fileId, target: fid, type: 'contains', direction: 'forward', weight: 1.0 });
    }
    for (const cls of (r.classes || [])) {
      const cid = `class:${file.path}:${cls.name}`;
      if (nodeIds.has(cid)) edges.push({ source: fileId, target: cid, type: 'contains', direction: 'forward', weight: 1.0 });
    }
    for (const ex of (r.exports || [])) {
      const eid = `function:${file.path}:${ex.name}`;
      const cid = `class:${file.path}:${ex.name}`;
      const target = nodeIds.has(eid) ? eid : nodeIds.has(cid) ? cid : null;
      if (target) edges.push({ source: fileId, target, type: 'exports', direction: 'forward', weight: 0.8 });
    }
    for (const targetPath of (batch.batchImportData?.[file.path] || [])) {
      edges.push({ source: fileId, target: `file:${targetPath}`, type: 'imports', direction: 'forward', weight: 0.7 });
    }
  }
  const filesSorted = [...batch.files].sort((a, b) => a.path.localeCompare(b.path));
  const parts = Math.ceil(Math.max(nodes.length / 60, edges.length / 120, 1));
  const groups = Array.from({ length: parts }, (_, i) => filesSorted.slice(Math.ceil(filesSorted.length / parts) * i, Math.ceil(filesSorted.length / parts) * (i + 1)));
  for (let i = 0; i < groups.length; i++) {
    const paths = new Set(groups[i].map(f => f.path));
    const partNodes = nodes.filter(n => paths.has(n.filePath));
    const partIds = new Set(partNodes.map(n => n.id));
    const partEdges = edges.filter(e => partIds.has(e.source));
    const filename = parts === 1 ? `batch-${batchIndex}.json` : `batch-${batchIndex}-part-${i + 1}.json`;
    fs.writeFileSync(path.join(intermediate, filename), JSON.stringify({ nodes: partNodes, edges: partEdges }, null, 2) + '\n');
  }
  console.log(`batch-${batchIndex}: ${parts} part(s), ${nodes.length} nodes, ${edges.length} edges`);
}
