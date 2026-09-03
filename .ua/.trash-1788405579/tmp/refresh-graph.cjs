const fs = require("fs");
const cp = require("child_process");

const root = process.cwd();
const graphPath = ".ua/knowledge-graph.json";
const graph = JSON.parse(fs.readFileSync(graphPath, "utf8"));
const scan = JSON.parse(fs.readFileSync(".ua/tmp/current-scan.json", "utf8"));
const structure = JSON.parse(fs.readFileSync(".ua/tmp/current-structure-output.json", "utf8"));
const importMap = JSON.parse(fs.readFileSync(".ua/tmp/current-import-map.json", "utf8")).importMap;
const changed = cp.execFileSync("git", ["diff", "6e0041aa7456da74ae96a10499e15a51dd046a5c..HEAD", "--name-only"], { encoding: "utf8" })
  .trim().split("\n").filter(Boolean);
for (const path of ["tests/test_scheduled_service.py", "tests/test_web_static.py", "web/repositories.py", "web/scheduler.py", "web/static/scheduled.js"]) {
  if (!changed.includes(path)) changed.push(path);
}

const fileTypes = new Set(["file", "config", "document", "service", "pipeline", "table", "schema", "resource", "endpoint"]);
const nodeForPath = new Map();
for (const node of graph.nodes) {
  if (fileTypes.has(node.type) && node.filePath && !nodeForPath.has(node.filePath)) nodeForPath.set(node.filePath, node);
}

const summaryOverrides = {
  "README.md": "项目入口文档，说明 TradingAgents 的多智能体金融分析能力，以及本地 Web 工作台、定时分析、配置和报告归档的使用方式。",
  "docs/superpowers/plans/2026-09-02-scheduled-analysis.md": "定时分析功能的实施计划，记录持久化、Cron 调度、并发控制、API、前端工作台和验证任务的交付状态。",
  "docs/superpowers/specs/2026-09-02-scheduled-analysis-design.md": "定时分析设计规格，定义每资产 Cron 任务、运行日志、参数推断、调度生命周期、并发准入、API 和 /scheduled 工作台的行为契约。",
  "pyproject.toml": "Python 项目与工具链配置，声明 FastAPI、APScheduler、Pytest、Ruff 及分析框架的运行和开发依赖。",
  "web/app.py": "FastAPI 应用装配入口，连接运行管理、持久化仓储、报告历史、关注列表和定时分析服务，并暴露调度任务、日志、设置和运行控制 API。",
  "web/repositories.py": "SQLite 仓储层，统一管理关注列表、分析运行、任务定义、定时运行日志、设置、快照和报告索引，并提供启动恢复所需的查询。",
  "web/manager.py": "Web 分析运行管理器，负责队列、容量与同资产准入、事件回放、终态恢复和重试，是手动分析与定时分析共享的执行边界。",
  "web/error_codes.py": "Web 运行与调度错误码及用户可见消息定义，覆盖容量不足、资产忙、跳过原因和终态恢复语义。",
  "web/migrations/005_scheduled_analysis.sql": "定时分析数据库迁移，创建 scheduled_jobs 与 scheduled_run_logs 表及查询索引，保存 Cron 任务、计划时间、触发结果和跳过原因。",
  "web/scheduled.py": "定时分析基础逻辑，严格校验五字段 Cron、计算带时区的下一次触发时间，并按最近成功运行、设置和默认目录推断可执行分析参数。",
  "web/scheduler.py": "APScheduler 调度服务，负责启停与无追赶恢复、任务注册、计划触发时间捕获、并发准入、运行日志状态推进以及服务重启后的未完成日志核对。",
  "web/static/scheduled.js": "定时分析工作台控制器，提供任务列表、新建编辑、Cron 预览、启停、立即运行、删除确认、日志展开、设置抽屉和可见状态轮询。",
};

function nodeType(meta) {
  if (meta.fileCategory === "docs") return "document";
  if (meta.fileCategory === "config") return "config";
  if (meta.fileCategory === "data" && meta.language === "sql") return "table";
  if (meta.fileCategory === "infra") return "service";
  return "file";
}
function prefixForType(type) { return type; }
function complexity(lines) { return lines > 200 ? "complex" : lines > 50 ? "moderate" : "simple"; }
function tagsFor(meta, type, path) {
  if (type === "document") return ["documentation", "design-spec", "project-artifact"];
  if (type === "config") return ["configuration", "project-artifact", "tradingagents"];
  if (type === "table") return ["database", "migration", "schema-definition"];
  if (path.startsWith("tests/")) return ["test", "regression", "scheduled-analysis"];
  if (path.endsWith(".js")) return ["frontend", "scheduler", "project-artifact"];
  return ["Web", "代码实现", "scheduled-analysis"];
}
function fileNode(meta) {
  const old = nodeForPath.get(meta.path);
  const type = old?.type || nodeType(meta);
  const id = old?.id || `${prefixForType(type)}:${meta.path}`;
  const name = old?.name || meta.path.split("/").pop();
  return {
    id, type, name, filePath: meta.path,
    summary: summaryOverrides[meta.path] || old?.summary || `覆盖 ${meta.path} 相关行为的代码、配置或文档，验证定时分析功能与现有 Web 平台的集成。`,
    tags: old?.tags?.length ? old.tags : tagsFor(meta, type, meta.path),
    complexity: complexity(meta.sizeLines),
  };
}

const structuralByPath = new Map((structure.results || []).map(result => [result.path, result]));
const removeIds = new Set();
for (const node of graph.nodes) if (changed.includes(node.filePath)) removeIds.add(node.id);
graph.nodes = graph.nodes.filter(node => !removeIds.has(node.id));
graph.edges = graph.edges.filter(edge => !removeIds.has(edge.source) && !removeIds.has(edge.target));

const currentMeta = new Map(scan.files.map(meta => [meta.path, meta]));
const changedFileIds = new Map();
for (const path of changed) {
  const meta = currentMeta.get(path);
  if (!meta) continue;
  const parent = fileNode(meta);
  graph.nodes.push(parent);
  changedFileIds.set(path, parent.id);
  const result = structuralByPath.get(path);
  if (!result) continue;
  const emitted = [];
  for (const fn of result.functions || []) {
    const id = `function:${path}:${fn.name}`;
    emitted.push({ id, type: "function", name: fn.name, filePath: path, lineRange: [fn.startLine, fn.endLine], summary: `位于 ${path} 的函数 ${fn.name}，负责定时分析流程中的专门职责。`, tags: ["函数", "Web", "scheduled-analysis"], complexity: complexity((fn.endLine || fn.startLine) - fn.startLine + 1) });
  }
  for (const cls of result.classes || []) {
    const id = `class:${path}:${cls.name}`;
    emitted.push({ id, type: "class", name: cls.name, filePath: path, lineRange: [cls.startLine, cls.endLine], summary: `位于 ${path} 的类 ${cls.name}，封装定时分析模块中的核心状态与行为。`, tags: ["类", "Web", "scheduled-analysis"], complexity: complexity((cls.endLine || cls.startLine) - cls.startLine + 1) });
  }
  graph.nodes.push(...emitted);
  for (const node of emitted) {
    graph.edges.push({ source: parent.id, target: node.id, type: "contains", direction: "forward", weight: 1 });
    if ((result.exports || []).some(exp => exp.name === node.name)) graph.edges.push({ source: parent.id, target: node.id, type: "exports", direction: "forward", weight: 0.8 });
  }
}

const pathToId = new Map();
for (const node of graph.nodes) if (fileTypes.has(node.type) && node.filePath) pathToId.set(node.filePath, node.id);
graph.edges = graph.edges.filter(edge => edge.type !== "imports" && edge.type !== "tested_by");
for (const [sourcePath, targets] of Object.entries(importMap)) {
  const source = pathToId.get(sourcePath);
  if (!source) continue;
  for (const targetPath of targets || []) {
    const target = pathToId.get(targetPath);
    if (target && target !== source) graph.edges.push({ source, target, type: "imports", direction: "forward", weight: 0.7 });
    if (sourcePath.startsWith("tests/") && target && !targetPath.startsWith("tests/")) graph.edges.push({ source: target, target: source, type: "tested_by", direction: "forward", weight: 0.5 });
  }
}

const layerByPath = {
  "README.md": "layer:documentation",
  "docs/superpowers/plans/2026-09-02-scheduled-analysis.md": "layer:documentation",
  "docs/superpowers/specs/2026-09-02-scheduled-analysis-design.md": "layer:documentation",
  "pyproject.toml": "layer:infrastructure",
  "web/app.py": "layer:web-service",
  "web/error_codes.py": "layer:web-service",
  "web/manager.py": "layer:web-service",
  "web/scheduled.py": "layer:web-service",
  "web/scheduler.py": "layer:web-service",
  "web/repositories.py": "layer:persistence",
  "web/migrations/005_scheduled_analysis.sql": "layer:persistence",
  "web/static/app.js": "layer:presentation",
  "web/static/i18n.js": "layer:presentation",
  "web/static/index.html": "layer:presentation",
  "web/static/scheduled.js": "layer:presentation",
  "web/static/styles.css": "layer:presentation",
};
for (const path of changed) if (path.startsWith("tests/")) layerByPath[path] = "layer:test";
const changedIds = new Set(changedFileIds.values());
for (const layer of graph.layers) layer.nodeIds = (layer.nodeIds || []).filter(id => !changedIds.has(id));
for (const [path, id] of changedFileIds) {
  const layer = graph.layers.find(item => item.id === layerByPath[path]);
  if (layer && !layer.nodeIds.includes(id)) layer.nodeIds.push(id);
}

const tourAdds = {
  "Web 运行生命周期": ["file:web/scheduler.py", "file:web/scheduled.py"],
  "持久化与报告": ["table:web/migrations/005_scheduled_analysis.sql"],
  "单页工作台": ["file:web/static/scheduled.js"],
  "测试与交付": ["file:tests/test_scheduled_api.py", "file:tests/test_scheduled_primitives.py", "file:tests/test_scheduled_repositories.py", "file:tests/test_scheduled_service.py", "document:docs/superpowers/plans/2026-09-02-scheduled-analysis.md"],
};
for (const step of graph.tour || []) for (const id of tourAdds[step.title] || []) if (graph.nodes.some(node => node.id === id) && !step.nodeIds.includes(id)) step.nodeIds.push(id);

const nodeIds = new Set(graph.nodes.map(node => node.id));
const edgeSeen = new Set();
graph.edges = graph.edges.filter(edge => {
  if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) return false;
  const key = `${edge.source}\u0000${edge.target}\u0000${edge.type}`;
  if (edgeSeen.has(key)) return false;
  edgeSeen.add(key); return true;
});
graph.project.analyzedAt = new Date().toISOString();
graph.project.gitCommitHash = cp.execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim();
fs.writeFileSync(graphPath, JSON.stringify(graph, null, 2) + "\n");
console.log(JSON.stringify({ changedFiles: changed.length, nodes: graph.nodes.length, edges: graph.edges.length, layers: graph.layers.length, tourSteps: graph.tour.length }, null, 2));
