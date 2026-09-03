#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    fail(`无法读取 JSON ${filePath}: ${error.message}`);
  }
}

function commonDirectoryPrefix(filePaths) {
  if (!filePaths.length) return [];
  const directoryParts = filePaths.map((filePath) => {
    const parts = filePath.split("/").filter(Boolean);
    return parts.length > 1 ? parts.slice(0, -1) : [];
  });
  const shortest = Math.min(...directoryParts.map((parts) => parts.length));
  const common = [];
  for (let index = 0; index < shortest; index += 1) {
    const value = directoryParts[0][index];
    if (directoryParts.every((parts) => parts[index] === value)) common.push(value);
    else break;
  }
  return common;
}

function flatGroupName(filePath) {
  const base = path.posix.basename(filePath).toLowerCase();
  if (/^(test_.*|.*(_test|\.test|\.spec)\.[^.]+)$/.test(base)) return "test";
  if (/config|settings|\.toml$|\.ya?ml$|\.json$/.test(base)) return "config";
  const extension = path.posix.extname(base).slice(1);
  return extension || "root";
}

function directoryGroup(filePath, commonPrefix, isFlat) {
  const parts = filePath.split("/").filter(Boolean);
  if (isFlat) return flatGroupName(filePath);
  const remaining = parts.slice(commonPrefix.length);
  return remaining.length > 1 ? remaining[0] : "root";
}

const directoryPatterns = [
  [/^(routes|api|controllers|endpoints|handlers|routers|serializers)$/i, "api"],
  [/^(services|core|lib|domain|logic|internal|mailers|jobs|channels|signals)$/i, "service"],
  [/^(models|db|data|persistence|repository|repositories|entities|entity|migrations|sql|database|schema)$/i, "data"],
  [/^(components|views|pages|ui|layouts|screens)$/i, "ui"],
  [/^(middleware|plugins|interceptors|guards)$/i, "middleware"],
  [/^(utils|helpers|common|shared|tools|pkg|templatetags)$/i, "utility"],
  [/^(config|constants|env|settings|management|commands)$/i, "config"],
  [/^(__tests__|test|tests|spec|specs)$/i, "test"],
  [/^(types|interfaces|schemas|contracts|dtos|dto|request|response)$/i, "types"],
  [/^hooks$/i, "hooks"],
  [/^(store|state|reducers|actions|slices)$/i, "state"],
  [/^(assets|static|public)$/i, "assets"],
  [/^(cmd|bin)$/i, "entry"],
  [/^(docs|documentation|wiki)$/i, "documentation"],
  [/^(deploy|deployment|infra|infrastructure|docker|k8s|kubernetes|helm|charts|terraform|tf)$/i, "infrastructure"],
  [/^(\.github|\.gitlab|\.circleci)$/i, "ci-cd"],
];

function filePattern(filePath) {
  const lower = filePath.toLowerCase();
  const base = path.posix.basename(lower);
  if (/^(test_.*\.py|.*_test\.go|.*test\.java|.*_spec\.rb|.*test\.php|.*tests\.cs)$/.test(base)
      || /\.(test|spec)\.[^.]+$/.test(base)) return "test";
  if (base.endsWith(".d.ts")) return "types";
  if (base === "__init__.py" || base === "index.ts" || base === "index.js") return "entry";
  if (base === "manage.py" || base === "config.ru") return "entry";
  if (/^(wsgi|asgi)\.py$/.test(base)) return "config";
  if (/^(cargo\.toml|go\.mod|gemfile|pom\.xml|build\.gradle|composer\.json|pyproject\.toml|requirements.*\.txt)$/.test(base)) return "config";
  if (/^dockerfile(\..+)?$/.test(base) || /^docker-compose.*\.ya?ml$/.test(base) || base === "makefile") return "infrastructure";
  if (/\.tf(vars)?$/.test(base)) return "infrastructure";
  if (lower.startsWith(".github/workflows/") || base === ".gitlab-ci.yml" || base === "jenkinsfile") return "ci-cd";
  if (base.endsWith(".sql")) return "data";
  if (/\.(graphql|gql|proto)$/.test(base)) return "types";
  if (/\.(md|rst)$/.test(base)) return "documentation";
  const segments = lower.split("/");
  for (const segment of segments.slice(0, -1)) {
    const match = directoryPatterns.find(([pattern]) => pattern.test(segment));
    if (match) return match[1];
  }
  return null;
}

function matchesRole(node, role) {
  const haystack = [node.filePath, node.name, node.summary, ...(node.tags || [])].join(" ").toLowerCase();
  if (role === "model") return /(model|模型|entity|实体|repository|仓储|storage|存储|dataflow|数据流)/.test(haystack);
  if (role === "api") return /(api|route|路由|endpoint|端点|fastapi|handler|处理器)/.test(haystack);
  return false;
}

function uniqueSorted(values) {
  return [...new Set(values)].sort();
}

function increment(map, key, amount = 1) {
  map.set(key, (map.get(key) || 0) + amount);
}

function main() {
  const [, , inputPath, outputPath] = process.argv;
  if (!inputPath || !outputPath) fail("用法: node ua-arch-analyze.js <input.json> <output.json>");
  const input = readJson(inputPath);
  const fileNodes = Array.isArray(input.fileNodes) ? input.fileNodes : [];
  const importEdges = Array.isArray(input.importEdges) ? input.importEdges : [];
  const allEdges = Array.isArray(input.allEdges) ? input.allEdges : [];
  if (!fileNodes.length) fail("输入不包含 fileNodes");

  const nodeById = new Map(fileNodes.map((node) => [node.id, node]));
  const filePaths = fileNodes.map((node) => node.filePath);
  const commonPrefix = commonDirectoryPrefix(filePaths);
  const isFlat = filePaths.every((filePath) => !filePath.includes("/"));
  const groupById = new Map();
  const directoryGroups = {};
  const nodeTypeGroups = {};
  const fileCatalog = {};

  for (const node of fileNodes) {
    const group = directoryGroup(node.filePath, commonPrefix, isFlat);
    groupById.set(node.id, group);
    (directoryGroups[group] ||= []).push(node.id);
    (nodeTypeGroups[node.type] ||= []).push(node.id);
    fileCatalog[node.id] = {
      filePath: node.filePath,
      type: node.type,
      summary: node.summary || "",
      tags: node.tags || [],
      directoryGroup: group,
      pattern: filePattern(node.filePath),
    };
  }
  for (const ids of Object.values(directoryGroups)) ids.sort();
  for (const ids of Object.values(nodeTypeGroups)) ids.sort();

  const fanIn = new Map(fileNodes.map((node) => [node.id, 0]));
  const fanOut = new Map(fileNodes.map((node) => [node.id, 0]));
  const adjacency = Object.fromEntries(fileNodes.map((node) => [node.id, []]));
  const importedGroups = new Map();
  const importedByGroups = new Map();
  const interGroupCounts = new Map();
  const internalCounts = new Map();
  const involvedCounts = new Map();

  for (const edge of importEdges) {
    if (!nodeById.has(edge.source) || !nodeById.has(edge.target)) continue;
    adjacency[edge.source].push(edge.target);
    increment(fanOut, edge.source);
    increment(fanIn, edge.target);
    const from = groupById.get(edge.source);
    const to = groupById.get(edge.target);
    (importedGroups.get(from) || importedGroups.set(from, new Set()).get(from)).add(to);
    (importedByGroups.get(to) || importedByGroups.set(to, new Set()).get(to)).add(from);
    increment(interGroupCounts, `${from}\u0000${to}`);
    if (from === to) {
      increment(internalCounts, from);
      increment(involvedCounts, from);
    } else {
      increment(involvedCounts, from);
      increment(involvedCounts, to);
    }
  }
  for (const targets of Object.values(adjacency)) targets.sort();

  const crossCounts = new Map();
  const nonCodeConnections = [];
  for (const edge of allEdges) {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) continue;
    increment(crossCounts, `${source.type}\u0000${target.type}\u0000${edge.type}`);
    if (source.type !== "file" || target.type !== "file") {
      nonCodeConnections.push({ source: edge.source, target: edge.target, type: edge.type });
    }
  }

  const interGroupImports = [...interGroupCounts.entries()]
    .map(([key, count]) => {
      const [from, to] = key.split("\u0000");
      return { from, to, count };
    })
    .sort((a, b) => b.count - a.count || a.from.localeCompare(b.from) || a.to.localeCompare(b.to));

  const intraGroupDensity = {};
  for (const group of Object.keys(directoryGroups).sort()) {
    const internalEdges = internalCounts.get(group) || 0;
    const totalEdges = involvedCounts.get(group) || 0;
    intraGroupDensity[group] = {
      internalEdges,
      totalEdges,
      density: totalEdges ? Number((internalEdges / totalEdges).toFixed(4)) : 0,
    };
  }

  const groupDependencies = {};
  for (const group of Object.keys(directoryGroups).sort()) {
    groupDependencies[group] = {
      importsFrom: uniqueSorted([...(importedGroups.get(group) || [])].filter((value) => value !== group)),
      importedBy: uniqueSorted([...(importedByGroups.get(group) || [])].filter((value) => value !== group)),
    };
  }

  const patternMatches = {};
  for (const group of Object.keys(directoryGroups).sort()) {
    const direct = directoryPatterns.find(([pattern]) => pattern.test(group));
    if (direct) patternMatches[group] = direct[1];
    else {
      const patterns = directoryGroups[group].map((id) => fileCatalog[id].pattern).filter(Boolean);
      const counts = new Map();
      patterns.forEach((pattern) => increment(counts, pattern));
      const winner = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
      if (winner && winner[1] / directoryGroups[group].length >= 0.5) patternMatches[group] = winner[0];
    }
  }

  const pathsLower = filePaths.map((filePath) => filePath.toLowerCase());
  const infraFiles = fileNodes
    .filter((node) => ["infrastructure", "ci-cd"].includes(filePattern(node.filePath)) || ["service", "pipeline", "resource"].includes(node.type))
    .map((node) => node.filePath)
    .sort();
  const deploymentTopology = {
    hasDockerfile: pathsLower.some((filePath) => /(^|\/)dockerfile(\..+)?$/.test(filePath)),
    hasCompose: pathsLower.some((filePath) => /(^|\/)docker-compose.*\.ya?ml$/.test(filePath)),
    hasK8s: pathsLower.some((filePath) => /(^|\/)(k8s|kubernetes|helm|charts)\//.test(filePath)),
    hasTerraform: pathsLower.some((filePath) => /\.tf(vars)?$/.test(filePath) || /(^|\/)terraform\//.test(filePath)),
    hasCI: pathsLower.some((filePath) => filePath.startsWith(".github/workflows/") || /(^|\/)(\.gitlab-ci\.yml|jenkinsfile)$/.test(filePath)),
    multiEnvironment: pathsLower.some((filePath) => /(dockerfile\.(dev|prod)|docker-compose\.(dev|prod)|\.env\.(dev|prod))/.test(filePath)),
    infraFiles,
  };

  const dataPipeline = {
    schemaFiles: fileNodes.filter((node) => node.type === "schema" || /(^|\/)(schema[^/]*\.(sql|graphql|gql|proto)|.*\.proto)$/.test(node.filePath.toLowerCase())).map((node) => node.filePath).sort(),
    migrationFiles: fileNodes.filter((node) => /(^|\/)migrations\/.*\.sql$/.test(node.filePath.toLowerCase())).map((node) => node.filePath).sort(),
    tableNodes: fileNodes.filter((node) => node.type === "table").map((node) => node.id).sort(),
    dataModelFiles: fileNodes.filter((node) => node.type === "file" && matchesRole(node, "model")).map((node) => node.filePath).sort(),
    apiHandlerFiles: fileNodes.filter((node) => node.type === "endpoint" || (node.type === "file" && matchesRole(node, "api"))).map((node) => node.filePath).sort(),
  };

  const groupNames = Object.keys(directoryGroups);
  const documentedGroups = new Set();
  for (const group of groupNames) {
    const groupIds = directoryGroups[group];
    if (groupIds.some((id) => /(^|\/)readme\.(md|rst)$/i.test(fileCatalog[id].filePath))) documentedGroups.add(group);
  }
  const documentNodes = fileNodes.filter((node) => node.type === "document" || /\.(md|rst)$/i.test(node.filePath));
  for (const doc of documentNodes) {
    const content = `${doc.filePath} ${doc.summary || ""}`.toLowerCase();
    for (const group of groupNames) {
      if (group !== "root" && content.includes(group.toLowerCase())) documentedGroups.add(group);
    }
  }
  const docCoverage = {
    groupsWithDocs: documentedGroups.size,
    totalGroups: groupNames.length,
    coverageRatio: Number((documentedGroups.size / groupNames.length).toFixed(4)),
    documentedGroups: [...documentedGroups].sort(),
    undocumentedGroups: groupNames.filter((group) => !documentedGroups.has(group)).sort(),
  };

  const pairNames = new Set();
  for (const row of interGroupImports) {
    if (row.from === row.to) continue;
    pairNames.add([row.from, row.to].sort().join("\u0000"));
  }
  const dependencyDirection = [];
  for (const pair of pairNames) {
    const [a, b] = pair.split("\u0000");
    const aToB = interGroupCounts.get(`${a}\u0000${b}`) || 0;
    const bToA = interGroupCounts.get(`${b}\u0000${a}`) || 0;
    if (aToB > bToA) dependencyDirection.push({ dependent: a, dependsOn: b, forwardCount: aToB, reverseCount: bToA });
    else if (bToA > aToB) dependencyDirection.push({ dependent: b, dependsOn: a, forwardCount: bToA, reverseCount: aToB });
    else dependencyDirection.push({ dependent: a, dependsOn: b, forwardCount: aToB, reverseCount: bToA, bidirectional: true });
  }
  dependencyDirection.sort((a, b) => b.forwardCount - a.forwardCount || a.dependent.localeCompare(b.dependent));

  const crossCategoryEdges = [...crossCounts.entries()]
    .map(([key, count]) => {
      const [fromType, toType, edgeType] = key.split("\u0000");
      return { fromType, toType, edgeType, count };
    })
    .sort((a, b) => b.count - a.count || a.fromType.localeCompare(b.fromType));

  const output = {
    scriptCompleted: true,
    commonPathPrefix: commonPrefix.length ? `${commonPrefix.join("/")}/` : "",
    directoryGroups,
    nodeTypeGroups,
    importAdjacency: adjacency,
    groupDependencies,
    crossCategoryEdges,
    nonCodeConnections,
    interGroupImports,
    intraGroupDensity,
    patternMatches,
    deploymentTopology,
    dataPipeline,
    docCoverage,
    dependencyDirection,
    fileStats: {
      totalFileNodes: fileNodes.length,
      filesPerGroup: Object.fromEntries(Object.entries(directoryGroups).map(([group, ids]) => [group, ids.length])),
      nodeTypeCounts: Object.fromEntries(Object.entries(nodeTypeGroups).map(([type, ids]) => [type, ids.length])),
    },
    fileFanIn: Object.fromEntries([...fanIn.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))),
    fileFanOut: Object.fromEntries([...fanOut.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))),
    fileCatalog,
  };

  try {
    fs.writeFileSync(outputPath, `${JSON.stringify(output, null, 2)}\n`);
  } catch (error) {
    fail(`无法写入结果 ${outputPath}: ${error.message}`);
  }
}

main();
