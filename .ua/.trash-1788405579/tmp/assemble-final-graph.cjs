#!/usr/bin/env node
const fs = require("fs");

const graph = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const rawLayers = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const rawTour = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
const scan = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
const layers = Array.isArray(rawLayers) ? rawLayers : rawLayers.layers || [];
const tour = Array.isArray(rawTour) ? rawTour : rawTour.steps || [];
const output = {
  version: "1.0.0",
  project: {
    name: scan.name,
    languages: scan.languages || [],
    frameworks: scan.frameworks || [],
    description: scan.description,
    analyzedAt: new Date().toISOString(),
    gitCommitHash: process.argv[7],
  },
  nodes: graph.nodes || [],
  edges: graph.edges || [],
  layers,
  tour,
};
fs.writeFileSync(process.argv[6], JSON.stringify(output, null, 2));
