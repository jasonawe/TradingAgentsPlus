#!/usr/bin/env node

const fs = require("node:fs");

const [, , resultsPath, outputPath] = process.argv;
if (!resultsPath || !outputPath) {
  process.stderr.write("用法: node ua-arch-assign.js <results.json> <layers.json>\n");
  process.exit(1);
}

const results = JSON.parse(fs.readFileSync(resultsPath, "utf8"));
const catalog = results.fileCatalog || {};

const definitions = [
  {
    id: "layer:agent-domain",
    name: "核心智能体层",
    description: "实现分析师、研究辩论、风险辩论、交易员与投资组合经理等多智能体金融研究角色及其共享状态和工具。",
  },
  {
    id: "layer:graph-orchestration",
    name: "图编排层",
    description: "负责 TradingAgents 工作流图的构建、条件路由、状态传播、反思循环、信号处理与执行计划。",
  },
  {
    id: "layer:market-data",
    name: "市场数据集成层",
    description: "统一接入行情、新闻、宏观、社交和预测市场数据源，并处理供应商路由、标的规范化、校验与本地化。",
  },
  {
    id: "layer:llm-integration",
    name: "LLM 适配层",
    description: "封装 OpenAI、Anthropic、Google、Azure、Bedrock 等模型提供商，并集中管理模型能力、密钥、超时和参数校验。",
  },
  {
    id: "layer:web-service",
    name: "Web 服务与 API 层",
    description: "提供 FastAPI 应用工厂、分析运行管理、恢复租约、错误分类、报告合成与 Web 请求响应模型。",
  },
  {
    id: "layer:presentation",
    name: "交互界面层",
    description: "承载命令行入口与本地 Web 单页工作台，包括行情图表、报告浏览、本地化、样式和用户交互。",
  },
  {
    id: "layer:persistence",
    name: "持久化与历史层",
    description: "管理 SQLite 模式迁移、仓储、分析产物、报告历史、快照、检查点恢复和智能体记忆。",
  },
  {
    id: "layer:test",
    name: "测试与验证层",
    description: "覆盖核心智能体、数据源、模型适配、CLI、Web API、前端模块及可靠性场景，并提供结构化输出冒烟验证。",
  },
  {
    id: "layer:infrastructure",
    name: "基础设施与配置",
    description: "定义 Python 打包依赖、环境变量模板、Docker 部署、GitHub Actions CI/CD 以及知识图谱分析配置与中间数据。",
  },
  {
    id: "layer:documentation",
    name: "文档与设计原型",
    description: "汇集项目说明、变更记录、设计规格、实施计划和界面探索原型，为平台演进提供决策依据。",
  },
];

const layerById = new Map(definitions.map((layer) => [layer.id, { ...layer, nodeIds: [] }]));

const persistenceFiles = new Set([
  "tradingagents/agents/utils/memory.py",
  "tradingagents/graph/checkpointer.py",
  "web/artifacts.py",
  "web/checkpoint_resume.py",
  "web/history.py",
  "web/repositories.py",
  "web/snapshots.py",
  "web/storage.py",
]);

const marketWebFiles = new Set([
  "web/market_data.py",
  "web/market_localization.py",
  "web/market_models.py",
  "web/providers/__init__.py",
  "web/providers/alpha_vantage_provider.py",
  "web/providers/yfinance_provider.py",
]);

function chooseLayer(id, file) {
  const filePath = file.filePath;

  if (filePath.startsWith("tests/") || filePath === "test.py" || filePath.startsWith("scripts/")
      || /^web\/static\/.*\.test\.js$/.test(filePath)) return "layer:test";

  if (filePath.startsWith("docs/") || filePath.startsWith(".superpowers/")
      || filePath === "README.md" || filePath === "CHANGELOG.md") return "layer:documentation";

  if (filePath.startsWith(".ua/") || filePath.startsWith(".github/")
      || [".dockerignore", "Dockerfile", "docker-compose.yml", ".env.example", ".env.enterprise.example", "pyproject.toml", "requirements.txt"].includes(filePath)
      || ["service", "pipeline", "config"].includes(file.type) && file.directoryGroup === "root") return "layer:infrastructure";

  if (filePath === "main.py" || filePath.startsWith("cli/")
      || filePath.startsWith("web/static/")) return "layer:presentation";

  if (file.type === "table" || filePath.startsWith("web/migrations/") || persistenceFiles.has(filePath)) return "layer:persistence";

  if (filePath.startsWith("tradingagents/llm_clients/")) return "layer:llm-integration";

  if (filePath.startsWith("tradingagents/dataflows/") || marketWebFiles.has(filePath)) return "layer:market-data";

  if (filePath.startsWith("tradingagents/graph/")) return "layer:graph-orchestration";

  if (filePath.startsWith("tradingagents/")) return "layer:agent-domain";

  if (filePath.startsWith("web/")) return "layer:web-service";

  throw new Error(`无法分层的节点: ${id} (${filePath})`);
}

for (const [id, file] of Object.entries(catalog)) {
  const layerId = chooseLayer(id, file);
  layerById.get(layerId).nodeIds.push(id);
}

const layers = definitions.map(({ id }) => layerById.get(id));
for (const layer of layers) layer.nodeIds.sort();

const inputIds = Object.keys(catalog).sort();
const assignedIds = layers.flatMap((layer) => layer.nodeIds);
const assignedSet = new Set(assignedIds);
const duplicates = assignedIds.filter((id, index) => assignedIds.indexOf(id) !== index);
const missing = inputIds.filter((id) => !assignedSet.has(id));
const unknown = assignedIds.filter((id) => !(id in catalog));
const empty = layers.filter((layer) => layer.nodeIds.length === 0).map((layer) => layer.id);

if (layers.length < 3 || layers.length > 10 || duplicates.length || missing.length || unknown.length || empty.length
    || assignedIds.length !== inputIds.length || assignedSet.size !== inputIds.length) {
  process.stderr.write(`${JSON.stringify({ layerCount: layers.length, duplicates, missing, unknown, empty, inputCount: inputIds.length, assignedCount: assignedIds.length, uniqueAssignedCount: assignedSet.size }, null, 2)}\n`);
  process.exit(1);
}

fs.writeFileSync(outputPath, `${JSON.stringify(layers, null, 2)}\n`);
process.stdout.write(`${JSON.stringify({ layerCount: layers.length, inputCount: inputIds.length, assignedCount: assignedIds.length, layers: layers.map((layer) => ({ id: layer.id, name: layer.name, count: layer.nodeIds.length })) }, null, 2)}\n`);
