#!/usr/bin/env node
const fs = require("fs");
const graphPath = process.argv[2];
const outputPath = process.argv[3];
try {
  const graph = JSON.parse(fs.readFileSync(graphPath, "utf8"));
  const issues = [], warnings = [];
  if (!Array.isArray(graph.nodes)) { issues.push("graph.nodes is missing or not an array"); graph.nodes = []; }
  if (!Array.isArray(graph.edges)) { issues.push("graph.edges is missing or not an array"); graph.edges = []; }
  const nodeIds = new Set();
  const seen = new Map();
  graph.nodes.forEach((node, index) => {
    if (!node.id) { issues.push(`Node[${index}] missing id`); return; }
    if (!node.type) issues.push(`Node[${index}] '${node.id}' missing type`);
    if (!node.name) issues.push(`Node[${index}] '${node.id}' missing name`);
    if (!node.summary) issues.push(`Node[${index}] '${node.id}' missing summary`);
    if (!node.tags || !node.tags.length) issues.push(`Node[${index}] '${node.id}' missing tags`);
    if (seen.has(node.id)) issues.push(`Duplicate node ID '${node.id}' at indices ${seen.get(node.id)} and ${index}`);
    else seen.set(node.id, index);
    nodeIds.add(node.id);
  });
  graph.edges.forEach((edge, index) => {
    if (!nodeIds.has(edge.source)) issues.push(`Edge[${index}] source '${edge.source}' not found`);
    if (!nodeIds.has(edge.target)) issues.push(`Edge[${index}] target '${edge.target}' not found`);
  });
  const fileTypes = new Set(["file", "config", "document", "service", "pipeline", "table", "schema", "resource", "endpoint"]);
  const fileNodes = graph.nodes.filter((node) => fileTypes.has(node.type)).map((node) => node.id);
  const assigned = new Map();
  if (!Array.isArray(graph.layers)) { if (graph.layers) warnings.push("graph.layers is not an array"); graph.layers = []; }
  if (!Array.isArray(graph.tour)) { if (graph.tour) warnings.push("graph.tour is not an array"); graph.tour = []; }
  graph.layers.forEach((layer) => {
    for (const id of layer.nodeIds || []) {
      if (!nodeIds.has(id)) issues.push(`Layer '${layer.id}' refs missing node '${id}'`);
      if (assigned.has(id)) issues.push(`Node '${id}' appears in multiple layers`);
      assigned.set(id, layer.id);
    }
  });
  fileNodes.forEach((id) => { if (!assigned.has(id)) issues.push(`File node '${id}' not in any layer`); });
  graph.tour.forEach((step, index) => {
    for (const id of step.nodeIds || []) {
      if (!nodeIds.has(id)) issues.push(`Tour step[${index}] refs missing node '${id}'`);
    }
  });
  const withEdges = new Set([...graph.edges.map((edge) => edge.source), ...graph.edges.map((edge) => edge.target)]);
  graph.nodes.forEach((node) => { if (!withEdges.has(node.id)) warnings.push(`Node '${node.id}' has no edges (orphan)`); });
  const stats = {
    totalNodes: graph.nodes.length,
    totalEdges: graph.edges.length,
    totalLayers: graph.layers.length,
    tourSteps: graph.tour.length,
    nodeTypes: graph.nodes.reduce((counts, node) => { counts[node.type] = (counts[node.type] || 0) + 1; return counts; }, {}),
    edgeTypes: graph.edges.reduce((counts, edge) => { counts[edge.type] = (counts[edge.type] || 0) + 1; return counts; }, {}),
  };
  fs.writeFileSync(outputPath, JSON.stringify({ issues, warnings, stats }, null, 2));
  process.exit(0);
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
}
