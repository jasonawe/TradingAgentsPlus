#!/usr/bin/env node
const fs = require("fs");

const graph = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const rawLayers = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const layers = Array.isArray(rawLayers) ? rawLayers : rawLayers.layers || [];
const fileTypes = new Set([
  "file",
  "config",
  "document",
  "service",
  "pipeline",
  "table",
  "schema",
  "resource",
  "endpoint",
]);
const nodes = graph.nodes
  .filter((node) => fileTypes.has(node.type))
  .map(({ id, type, name, filePath, summary }) => ({
    id,
    type,
    name,
    filePath,
    summary,
  }));
const nodeIds = new Set(nodes.map((node) => node.id));
const payload = {
  nodes,
  edges: graph.edges.filter(
    (edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target),
  ),
  layers: layers.map(({ id, name, description }) => ({
    id,
    name,
    description,
  })),
};
fs.writeFileSync(process.argv[4], JSON.stringify(payload, null, 2));
