#!/usr/bin/env node
const fs = require("fs");

try {
  const input = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const ids = new Set(input.nodes.map((node) => node.id));
  const incoming = new Map(input.nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(input.nodes.map((node) => [node.id, 0]));
  for (const edge of input.edges) {
    if (!ids.has(edge.source) || !ids.has(edge.target)) continue;
    outgoing.set(edge.source, (outgoing.get(edge.source) || 0) + 1);
    incoming.set(edge.target, (incoming.get(edge.target) || 0) + 1);
  }
  const byId = new Map(input.nodes.map((node) => [node.id, node]));
  const rank = (counts, key) => [...counts]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 20)
    .map(([id, value]) => ({ id, [key]: value, name: byId.get(id)?.name || id }));
  const entries = input.nodes
    .map((node) => {
      const path = node.filePath || "";
      const name = node.name || "";
      let score = 0;
      if (node.type === "document" && path === "README.md") score += 5;
      else if (node.type === "document" && !path.includes("/") && /\.md$/i.test(path)) score += 2;
      if (["main.py", "app.py", "run.py", "__main__.py", "index.js"].includes(name)) score += 3;
      if ((path.match(/\//g) || []).length <= 1) score += 1;
      if ((outgoing.get(node.id) || 0) >= 5) score += 1;
      if ((incoming.get(node.id) || 0) === 0) score += 1;
      return { id: node.id, score, name, summary: node.summary };
    })
    .sort((a, b) => b.score - a.score || a.id.localeCompare(b.id))
    .slice(0, 5);
  const startNode = entries.find((entry) => byId.get(entry.id)?.type === "file")?.id || null;
  const adjacency = new Map(input.nodes.map((node) => [node.id, []]));
  for (const edge of input.edges) {
    if (["imports", "calls"].includes(edge.type) && adjacency.has(edge.source) && ids.has(edge.target)) {
      adjacency.get(edge.source).push(edge.target);
    }
  }
  const order = [], depthMap = {}, queue = startNode ? [[startNode, 0]] : [], seen = new Set();
  while (queue.length) {
    const [id, depth] = queue.shift();
    if (seen.has(id)) continue;
    seen.add(id);
    order.push(id);
    depthMap[id] = depth;
    for (const target of adjacency.get(id) || []) queue.push([target, depth + 1]);
  }
  const byDepth = {};
  for (const id of order) (byDepth[depthMap[id]] ||= []).push(id);
  const categories = { documentation: [], infrastructure: [], data: [], config: [] };
  for (const node of input.nodes) {
    const item = { id: node.id, name: node.name, type: node.type, summary: node.summary };
    if (node.type === "document") categories.documentation.push(item);
    else if (["service", "pipeline", "resource"].includes(node.type)) categories.infrastructure.push(item);
    else if (["table", "schema", "endpoint"].includes(node.type)) categories.data.push(item);
    else if (node.type === "config") categories.config.push(item);
  }
  const output = {
    scriptCompleted: true,
    entryPointCandidates: entries,
    fanInRanking: rank(incoming, "fanIn"),
    fanOutRanking: rank(outgoing, "fanOut"),
    bfsTraversal: { startNode, order, depthMap, byDepth },
    nonCodeFiles: categories,
    clusters: [],
    layers: { count: input.layers.length, list: input.layers },
    nodeSummaryIndex: Object.fromEntries(input.nodes.map((node) => [node.id, { name: node.name, type: node.type, summary: node.summary }])),
    totalNodes: input.nodes.length,
    totalEdges: input.edges.length,
  };
  fs.writeFileSync(process.argv[3], JSON.stringify(output, null, 2));
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
}
